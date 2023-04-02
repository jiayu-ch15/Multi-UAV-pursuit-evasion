from functorch import vmap

import omni.isaac.core.utils.torch as torch_utils
import torch
from omni.isaac.core.objects import DynamicCuboid
from tensordict.tensordict import TensorDict, TensorDictBase
from torchrl.data import CompositeSpec, UnboundedContinuousTensorSpec

import omni_drones.utils.kit as kit_utils
import omni_drones.utils.scene as scene_utils

from omni_drones.envs.isaac_env import AgentSpec, IsaacEnv
from omni_drones.views import RigidPrimView
from omni_drones.utils.torch import cpos, off_diag
from omni_drones.robots.assembly.transportation_group import TransportationGroup
from omni_drones.robots.config import RobotCfg
from omni_drones.robots.drone import MultirotorBase


class Transport(IsaacEnv):
    def __init__(self, cfg, headless):
        super().__init__(cfg, headless)
        self.reward_effort_weight = self.cfg.task.get("reward_effort_weight", 0.1)
        self.reward_distance_scale = self.cfg.task.get("reward_distance_scale", 0.5)
        self.safe_distance = self.cfg.task.get("safe_distance", 0.5)

        self.group.initialize()
        self.payload = self.group.payload_view
        self.payload_mass_dist = torch.distributions.Uniform(
            torch.as_tensor(self.cfg.task.payload_mass_min, device=self.device),
            torch.as_tensor(self.cfg.task.payload_mass_max, device=self.device)
        )
        self.payload_target_visual = RigidPrimView(
            "/World/envs/.*/payloadTargetVis",
            reset_xform_properties=False
        )
        self.payload_target_visual.initialize()
        
        self.init_poses = self.group.get_world_poses(clone=True)
        self.init_velocities = torch.zeros_like(self.group.get_velocities())
        self.init_joint_pos = self.group.get_joint_positions(clone=True)
        self.init_joint_vel = torch.zeros_like(self.group.get_joint_velocities())

        self.init_drone_poses = self.drone.get_world_poses(clone=True)
        self.init_drone_vels = torch.zeros_like(self.drone.get_velocities())

        drone_state_dim = self.drone.state_spec.shape[0]
        observation_spec = CompositeSpec({
            "self": UnboundedContinuousTensorSpec((1, drone_state_dim)).to(self.device),
            "others": UnboundedContinuousTensorSpec((self.drone.n-1, 4)).to(self.device),
            "payload": UnboundedContinuousTensorSpec((1, 31)).to(self.device)
        })

        state_spec = CompositeSpec(
            drones=UnboundedContinuousTensorSpec((self.drone.n, drone_state_dim)).to(self.device),
            payload=UnboundedContinuousTensorSpec((1, 28)).to(self.device)
        )

        self.agent_spec["drone"] = AgentSpec(
            "drone",
            4,
            observation_spec,
            self.drone.action_spec.to(self.device),
            UnboundedContinuousTensorSpec(1).to(self.device),
            state_spec=state_spec
        )

        self.payload_target_pos = torch.zeros(self.num_envs, 3, device=self.device)
        self.payload_target_axis = torch.zeros(self.num_envs, 3, 3, device=self.device)
        self.payload_mass = torch.zeros(self.num_envs, 1, device=self.device)

        self.target_pos_scale = torch.tensor([4., 4., 1.], device=self.device)
        self.target_pos_translation = torch.tensor([-2., -2., .1], device=self.device)

        info_spec = CompositeSpec(
            payload_mass=UnboundedContinuousTensorSpec((1,)),
        ).expand(self.num_envs).to(self.device)

        self.observation_spec["info"] = info_spec

    def _design_scene(self):
        cfg = RobotCfg()
        drone_model = MultirotorBase.REGISTRY[self.cfg.task.drone_model]
        self.drone: MultirotorBase = drone_model(cfg=cfg)
        self.group = TransportationGroup(drone=self.drone)

        scene_utils.design_scene()

        self.group.spawn(translations=[(0, 0, 2.0)])
        DynamicCuboid(
            "/World/envs/env_0/payloadTargetVis",
            scale=torch.tensor([0.5, 0.5, 0.2]),
            color=torch.tensor([0.8, 0.1, 0.1]),
            size=2.01,
        )
        kit_utils.set_collision_properties(
            "/World/envs/env_0/payloadTargetVis",
            collision_enabled=False
        )
        kit_utils.set_rigid_body_properties(
            "/World/envs/env_0/payloadTargetVis",
            disable_gravity=True
        )

        return ["/World/defaultGroundPlane"]

    def _reset_idx(self, env_ids: torch.Tensor):
        pos, rot = self.init_poses

        self.group._reset_idx(env_ids)
        self.group.set_world_poses(pos[env_ids], rot[env_ids], env_ids)
        self.group.set_velocities(self.init_velocities[env_ids], env_ids)

        self.group.set_joint_positions(self.init_joint_pos[env_ids], env_ids)
        self.group.set_joint_velocities(self.init_joint_vel[env_ids], env_ids)

        payload_target_pos = (
            torch.rand(len(env_ids), 3, device=self.device)
            * self.target_pos_scale
            + self.target_pos_translation
        )

        payload_target_rpy = torch.zeros(len(env_ids), 3, device=self.device)
        payload_target_rpy[..., 2] = (
            torch.pi * 2 * torch.rand(len(env_ids), device=self.device)
        )

        payload_target_rot = torch_utils.quat_from_euler_xyz(
            *payload_target_rpy.unbind(-1)
        )

        self.payload_target_pos[env_ids] = payload_target_pos
        target_axis = vmap(get_axis)(payload_target_rot, payload_target_pos)
        self.payload_target_axis[env_ids] = target_axis

        payload_masses = self.payload_mass_dist.sample(env_ids.shape)
        self.payload.set_masses(payload_masses, env_ids)

        payload_target_pose = (payload_target_pos + self.envs_positions[env_ids], payload_target_rot)
        self.payload_target_visual.set_world_poses(*payload_target_pose, env_ids)

    def _pre_sim_step(self, tensordict: TensorDictBase):
        actions = tensordict[("action", "drone.action")]
        self.effort = self.drone.apply_action(actions)

    def _compute_state_and_obs(self):
        self.drone_states = self.drone.get_state()
        drone_pos = self.drone_states[..., :3]
        self.payload_pos, self.payload_rot = self.get_env_poses(self.payload.get_world_poses())
        payload_vels = self.payload.get_velocities()

        self.drone_rpos = vmap(cpos)(drone_pos, drone_pos)
        self.drone_rpos = vmap(off_diag)(self.drone_rpos)
        self.drone_pdist = torch.norm(self.drone_rpos, dim=-1, keepdim=True)

        payload_drone_rpos = self.payload_pos.unsqueeze(1) - drone_pos
        self.target_payload_rpos = self.payload_target_pos - self.payload_pos
        self.payload_axis = vmap(get_axis)(self.payload_rot, self.payload_pos)
        self.target_payload_raxis = (self.payload_target_axis - self.payload_axis).reshape(self.num_envs, 9)

        self.payload_heading: torch.Tensor = torch_utils.quat_axis(self.payload_rot, axis=0)
        self.payload_up: torch.Tensor = torch_utils.quat_axis(self.payload_rot, axis=2)

        payload_state = torch.cat(
            [
                self.target_payload_rpos,  # 3
                self.target_payload_raxis,  # 9
                self.payload_rot,  # 4
                payload_vels,  # 6
                self.payload_heading,  # 3
                self.payload_up, # 3
            ],
            dim=-1,
        ).unsqueeze(1)

        obs = TensorDict(
            {
                "self": self.drone_states.unsqueeze(2),  # [num_envs, drone.n, 1, *]
                "others": torch.cat(
                    [
                        self.drone_rpos,
                        self.drone_pdist,
                    ],
                    dim=-1,
                ),  # [num_envs, drone.n, drone.n-1, *]
                "payload": torch.cat(
                    [
                        payload_drone_rpos,  # 3
                        payload_state.expand(-1, self.drone.n, -1)
                    ],
                    dim=-1,
                ).unsqueeze(2),  # [num_envs, drone.n, 1, 28]
            },
            [self.num_envs, self.drone.n],
        )

        state = TensorDict(
            {
                "payload": payload_state,  # [num_envs, 1, 25]
                "drones": torch.cat(
                    [-payload_drone_rpos, self.drone_states[..., 3:]], dim=-1
                ),  # [num_envs, drone.n, *]
            },
            self.num_envs,
        )
        
        if self._should_render(0):
            pass

        return TensorDict({"drone.obs": obs, "drone.state": state}, self.num_envs)

    def _compute_reward_and_done(self):
        distance = torch.norm(
            torch.cat([self.target_payload_rpos, self.target_payload_raxis], dim=-1)
        , dim=-1, keepdim=True)
        separation = self.drone_pdist.min(dim=-2).values.min(dim=-2).values

        reward = torch.ones(self.num_envs, self.drone.n, 1, device=self.device)
        reward_pose = 1 / (1 + torch.square(distance * self.reward_distance_scale))
        reward_effort = self.reward_effort_weight * torch.exp(-self.effort).mean(-1, keepdim=True)
        reward_separation = torch.square(separation / self.safe_distance).clamp(0, 1)

        reward[:] = (reward_separation * (reward_pose + reward_effort)).unsqueeze(-1)

        done_hasnan = torch.isnan(self.drone_states).any(-1)
        done_fall = self.drone_states[..., 2] < 0.2

        done = (
            (self.progress_buf >= self.max_episode_length).unsqueeze(-1) 
            | done_fall.any(-1, keepdim=True)
            | done_hasnan.any(-1, keepdim=True)
        )

        self._tensordict["return"] += reward
        return TensorDict(
            {
                "reward": {"drone.reward": reward},
                "return": self._tensordict["return"],
                "done": done,
            },
            self.batch_size,
        )

def get_axis(rot: torch.Tensor, pos: torch.Tensor):
    device = rot.device
    offset = torch_utils.quat_rotate(rot.expand(3, -1), torch.eye(3, device=device)) 
    return pos.unsqueeze(1) + offset

