import torch
import abc
import os.path as osp
from contextlib import contextmanager
from torchrl.data import TensorSpec

import omni.timeline
import omni.isaac.core.utils.prims as prim_utils
import omni_drones.utils.kit as kit_utils
from omni.isaac.core.simulation_context import SimulationContext
from omni.isaac.core.articulations import ArticulationView

from omni_drones.robots.config import (
    RobotCfg,
    RigidBodyPropertiesCfg,
    ArticulationRootPropertiesCfg,
)

ASSET_PATH = osp.join(osp.dirname(__file__), "assets")
TEMPLATE_PRIM_PATH = "/World/envs/env_0"

class RobotBase(abc.ABC):

    usd_path: str
    prim_type: str = "Xform"
    prim_attributes: dict = None
    state_spec: TensorSpec
    action_spec: TensorSpec

    _robots = {}
    _envs_positions: torch.Tensor

    def __init__(self, name: str, cfg: RobotCfg=None) -> None:
        if name in RobotBase._robots:
            raise RuntimeError
        RobotBase._robots[name] = self
        if cfg is None:
            cfg = RobotCfg()
        self.name = name
        self.rigid_props: RigidBodyPropertiesCfg = cfg.rigid_props
        self.articulation_props: ArticulationRootPropertiesCfg = cfg.articulation_props
        
        self._count = 0
        
        if SimulationContext._instance is None:
            raise RuntimeError("The SimulationContext is not created.")

        self.device = SimulationContext._instance._device
        self.dt = SimulationContext._instance.get_physics_dt()

    def spawn(
        self, n: int=1, translation=(0., 0., 0.5)
    ):
        if SimulationContext._instance._physics_sim_view is not None:
            raise RuntimeError(
                "Cannot spawn robots after simulation_context.reset() is called."
            )
        translation = torch.atleast_2d(torch.as_tensor(translation, device=self.device))
        if n != len(translation):
            raise ValueError
        for i in range(self._count, self._count + n):
            prim_path = f"{TEMPLATE_PRIM_PATH}/{self.name}_{i}"
            if prim_utils.is_prim_path_valid(prim_path):
                raise RuntimeError(
                    f"Duplicate prim at {prim_path}."
                )
            prim_utils.create_prim(
                prim_path,
                prim_type=self.prim_type,
                usd_path=self.usd_path,
                translation=translation[i],
                attributes=self.prim_attributes,
            )
            # apply rigid body properties
            kit_utils.set_nested_rigid_body_properties(
                prim_path,
                linear_damping=self.rigid_props.linear_damping,
                angular_damping=self.rigid_props.angular_damping,
                max_linear_velocity=self.rigid_props.max_linear_velocity,
                max_angular_velocity=self.rigid_props.max_angular_velocity,
                max_depenetration_velocity=self.rigid_props.max_depenetration_velocity,
                enable_gyroscopic_forces=True,
                disable_gravity=self.rigid_props.disable_gravity,
                retain_accelerations=self.rigid_props.retain_accelerations,
            )
            # articulation root settings
            kit_utils.set_articulation_properties(
                prim_path,
                enable_self_collisions=self.articulation_props.enable_self_collisions,
                solver_position_iteration_count=self.articulation_props.solver_position_iteration_count,
                solver_velocity_iteration_count=self.articulation_props.solver_velocity_iteration_count,
            )

        self._count += n

    def initialize(self):
        if SimulationContext._instance._physics_sim_view is None:
            raise RuntimeError(
                "Cannot create ArticulationView before the simulation context resets."
                "Call simulation_context.reset() first."
            )
        prim_paths_expr = f"/World/envs/.*/{self.name}_*"
        # create handles
        # -- robot articulation
        self.articulations = ArticulationView(
            prim_paths_expr, reset_xform_properties=False
        )
        self.articulations.initialize()
        # set the default state
        self.articulations.post_reset()
        self.shape = (
            torch.arange(self.articulations.count)
            .reshape(-1, self._count).shape
        )
        self._physics_view = self.articulations._physics_view
        self._physics_sim_view = self.articulations._physics_sim_view
        
        if hasattr(self, "_envs_positions"):
            pos, rot = self.get_world_poses()
            self.set_env_poses(pos, rot)

        print(self.articulations._dof_names)
        print(self.articulations._dof_types)
        print(self.articulations._dofs_infos)

    @abc.abstractmethod
    def apply_action(self, actions: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError

    @abc.abstractmethod
    def _reset_idx(self, mask: torch.Tensor):
        raise NotImplementedError

    def get_world_poses(self, clone=True):
        with self._disable_warnings():
            poses = torch.unflatten(self._physics_view.get_root_transforms(), 0, self.shape)
            if clone:
                poses = poses.clone()
        return poses[..., :3], poses[..., [6, 3, 4 ,5]]

    def get_env_poses(self, clone=True):
        with self._disable_warnings():
            poses = torch.unflatten(self._physics_view.get_root_transforms(), 0, self.shape)
            if clone:
                poses = poses.clone()
        poses[..., :3] -= self._envs_positions
        return poses[..., :3], poses[..., [6, 3, 4, 5]]
        
    def set_env_poses(self, 
        positions: torch.Tensor, 
        orientations: torch.Tensor,
        indices: torch.Tensor=None,
    ):
        with self._disable_warnings():
            positions = (positions + self._envs_positions[indices]).flatten(0, -2)
            orientations = orientations.reshape(-1, 4)[:, [1, 2, 3, 0]]
            old_pose = self._physics_view.get_root_transforms().clone()
            indices = self._resolve_indices(indices)
            if positions is None:
                positions = old_pose[indices, :3]
            if orientations is None:
                orientations = old_pose[indices, 3:]
            new_pose = torch.cat([positions, orientations], dim=-1)
            old_pose[indices] = new_pose
            self._physics_view.set_root_transforms(old_pose, indices)

    def get_velocities(self, clone=True):
        with self._disable_warnings():
            velocities = torch.unflatten(self._physics_view.get_root_velocities(), 0, self.shape)
            if clone:
                velocities = velocities.clone()
        return velocities

    def set_velocities(
        self, 
        velocities: torch.Tensor, 
        indices: torch.Tensor=None
    ):
        with self._disable_warnings():
            velocities = velocities.flatten(0, -2)
            indices = self._resolve_indices(indices)
            root_vel = self._physics_view.get_root_velocities()
            root_vel[indices] = velocities
            self._physics_view.set_root_velocities(root_vel, indices)
    
    def get_joint_positions(self, clone=True):
        with self._disable_warnings():
            joint_positions = torch.unflatten(self._physics_view.get_dof_positions(), 0, self.shape)
            if clone:
                joint_positions = joint_positions.clone()
        return joint_positions

    def set_joint_positions(self, ):
        with self._disable_warnings():
            ...

    def get_joint_velocities(self, clone=True):
        with self._disable_warnings():
            joint_velocities = torch.unflatten(self._physics_view.get_dof_velocities(), 0, self.shape)
            if clone:
                joint_velocities = joint_velocities.clone()
        return joint_velocities

    def set_joint_velocities(self, ):
        with self._disable_warnings():
            ...

    def _resolve_indices(self, indices: torch.Tensor=None):
        all_indices = torch.arange(self.articulations.count, device=self.device).reshape(self.shape)
        if indices is None:
            indices = all_indices
        else:
            indices = all_indices[indices]
        return indices.flatten()

    @contextmanager
    def _disable_warnings(self):
        if not omni.timeline.get_timeline_interface().is_stopped() and self._physics_view is not None:
            try:
                self._physics_sim_view.enable_warnings(False)
                yield
            finally:
                self._physics_sim_view.enable_warnings(True)
        else:
            raise RuntimeError

    