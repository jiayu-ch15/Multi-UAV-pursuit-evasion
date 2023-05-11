from functorch import vmap
import torch
import torch.distributions as D
from tensordict.tensordict import TensorDict, TensorDictBase
from torchrl.data import CompositeSpec, UnboundedContinuousTensorSpec

import omni.isaac.core.utils.torch as torch_utils
import omni.isaac.core.utils.prims as prim_utils
import omni.physx.scripts.utils as script_utils
from omni.isaac.core.objects import DynamicCuboid
from pxr import UsdPhysics

import omni_drones.utils.kit as kit_utils
import omni_drones.utils.scene as scene_utils
from omni_drones.utils.torch import euler_to_quaternion

from omni_drones.envs.isaac_env import AgentSpec, IsaacEnv
from omni_drones.views import RigidPrimView
from omni_drones.utils.torch import cpos, off_diag, others
from omni_drones.robots.assembly.transportation_group import TransportationGroup
from omni_drones.robots.config import RobotCfg
from omni_drones.robots.drone import MultirotorBase

def create_obstacles(
    prim_path: str,
    translation=(0., 0., 1.5),
    width: float=5,
):
    prim = prim_utils.create_prim(
        prim_path=prim_path,
        prim_type="Capsule",
        translation=translation,
        attributes={"axis":"Y", "radius":0.05, "height": width}
    )
    UsdPhysics.RigidBodyAPI.Apply(prim)
    UsdPhysics.CollisionAPI.Apply(prim)
    kit_utils.set_collision_properties(
        prim_path, contact_offset=0.02, rest_offset=0
    )

    stage = prim_utils.get_current_stage()
    script_utils.createJoint(stage, "Fixed", prim.GetParent(), prim)
    return prim

class TransportFlyThrough(IsaacEnv):
    def __init__(self, cfg, headless):
        super().__init__(cfg, headless)
        self.reward_effort_weight = self.cfg.task.reward_effort_weight
        self.reward_distance_scale = self.cfg.task.reward_distance_scale
        self.safe_distance = self.cfg.task.safe_distance
        self.reset_on_collision = self.cfg.task.reset_on_collision

        self.group.initialize(track_contact_forces=True)
        self.payload = self.group.payload_view

        self.obstacles = RigidPrimView(
            "/World/envs/env_*/obstacle_*",
            reset_xform_properties=False,
            shape=[self.num_envs, -1],
            track_contact_forces=True
        )
        self.obstacles.initialize()
        
        self.payload_target_visual = RigidPrimView(
            "/World/envs/.*/payloadTargetVis",
            reset_xform_properties=False
        )
        self.payload_target_visual.initialize()
        
        self.init_poses = self.group.get_world_poses(clone=True)
        self.init_velocities = torch.zeros_like(self.group.get_velocities())
        self.init_joint_pos = self.group.get_joint_positions(clone=True)
        self.init_joint_vel = torch.zeros_like(self.group.get_joint_velocities())
        self.obstacle_pos = self.get_env_poses(self.obstacles.get_world_poses())[0]

        self.init_drone_poses = self.drone.get_world_poses(clone=True)
        self.init_drone_vels = torch.zeros_like(self.drone.get_velocities())

        drone_state_dim = self.drone.state_spec.shape[0]
        observation_spec = CompositeSpec({
            "self": UnboundedContinuousTensorSpec((1, drone_state_dim)).to(self.device),
            "others": UnboundedContinuousTensorSpec((self.drone.n-1, drone_state_dim+1)).to(self.device),
            "payload": UnboundedContinuousTensorSpec((1, 19)).to(self.device),
            "obstacles": UnboundedContinuousTensorSpec((2, 2)).to(self.device),
        })

        state_spec = CompositeSpec({
            "drones": UnboundedContinuousTensorSpec((self.drone.n, drone_state_dim)).to(self.device),
            "payload": UnboundedContinuousTensorSpec((1, 19)).to(self.device),
            "obstacles": UnboundedContinuousTensorSpec((2, 2)).to(self.device),
        })

        self.agent_spec["drone"] = AgentSpec(
            "drone",
            4,
            observation_spec,
            self.drone.action_spec.to(self.device),
            UnboundedContinuousTensorSpec(1).to(self.device),
            state_spec=state_spec
        )


        self.payload_mass_dist = D.Uniform(
            torch.as_tensor(self.cfg.task.payload_mass_min, device=self.device),
            torch.as_tensor(self.cfg.task.payload_mass_max, device=self.device)
        )
        self.init_pos_dist = D.Uniform(
            torch.tensor([-2.5, -.5, 1.0], device=self.device),
            torch.tensor([-1.5, 0.5, 2.0], device=self.device)
        )
        self.init_rpy_dist = D.Uniform(
            torch.tensor([0., 0., -.5], device=self.device) * torch.pi,
            torch.tensor([0., 0., 0.5], device=self.device) * torch.pi
        )

        self.alpha = 0.7
        
        info_spec = CompositeSpec({
            "payload_mass": UnboundedContinuousTensorSpec(1),
        }).expand(self.num_envs).to(self.device)
        stats_spec = CompositeSpec({
            "payload_pos_error": UnboundedContinuousTensorSpec(1),
            "collision": UnboundedContinuousTensorSpec(1),
        }).expand(self.num_envs).to(self.device)
        self.observation_spec["info"] = info_spec
        self.observation_spec["stats"] = stats_spec
        self.info = TensorDict({
            "payload_mass": torch.zeros((self.num_envs, 1), device=self.device),
        }, [])
        self.stats = TensorDict({
            "payload_pos_error": torch.zeros((self.num_envs, 1), device=self.device),
            "collision": torch.zeros((self.num_envs, 1), device=self.device),
        }, [])

    def _design_scene(self):
        drone_model = MultirotorBase.REGISTRY[self.cfg.task.drone_model]
        cfg = drone_model.cfg_cls()
        self.drone: MultirotorBase = drone_model(cfg=cfg)
        self.group = TransportationGroup(drone=self.drone)

        scene_utils.design_scene()

        obstacle_spacing = self.cfg.task.obstacle_spacing
        create_obstacles(
            "/World/envs/env_0/obstacle_0", 
            translation=(0., 0., 1.3)
        )
        create_obstacles(
            "/World/envs/env_0/obstacle_1", 
            translation=(0., 0., 1.3+obstacle_spacing)
        )

        self.payload_target_pos = torch.tensor([1.5, 0., 1.5], device=self.device)

        DynamicCuboid(
            "/World/envs/env_0/payloadTargetVis",
            translation=self.payload_target_pos,
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
        self.group.spawn(translations=[(0, 0, 2.0)], enable_collision=True)

        return ["/World/defaultGroundPlane"]

    def _reset_idx(self, env_ids: torch.Tensor):
        pos = self.init_pos_dist.sample(env_ids.shape)
        rpy = self.init_rpy_dist.sample(env_ids.shape)
        rot = euler_to_quaternion(rpy)
        up = torch_utils.quat_axis(rot, 2)

        self.group._reset_idx(env_ids)
        self.group.set_world_poses(pos + self.envs_positions[env_ids], rot, env_ids)
        self.group.set_velocities(self.init_velocities[env_ids], env_ids)

        self.group.set_joint_positions(self.init_joint_pos[env_ids], env_ids)
        self.group.set_joint_velocities(self.init_joint_vel[env_ids], env_ids)

        payload_masses = self.payload_mass_dist.sample(env_ids.shape)
        self.info["payload_mass"][env_ids] = payload_masses.unsqueeze(-1).clone()

        self.payload.set_masses(payload_masses, env_ids)
        self.stats["payload_pos_error"][env_ids] = 0
        self.stats["collision"][env_ids] = 0


    def _pre_sim_step(self, tensordict: TensorDictBase):
        actions = tensordict[("action", "drone.action")]
        self.effort = self.drone.apply_action(actions)

    def _compute_state_and_obs(self):
        self.drone_states = self.drone.get_state()
        drone_pos = self.drone_states[..., :3]

        self.payload_pos, self.payload_rot = self.get_env_poses(self.payload.get_world_poses())
        payload_vels = self.payload.get_velocities()
        self.payload_heading: torch.Tensor = torch_utils.quat_axis(self.payload_rot, axis=0)
        self.payload_up: torch.Tensor = torch_utils.quat_axis(self.payload_rot, axis=2)
        
        self.drone_rpos = vmap(cpos)(drone_pos, drone_pos)
        self.drone_rpos = vmap(off_diag)(self.drone_rpos)
        self.drone_pdist = torch.norm(self.drone_rpos, dim=-1, keepdim=True)
        payload_drone_rpos = self.payload_pos.unsqueeze(1) - drone_pos

        self.target_payload_rpos = self.payload_target_pos - self.payload_pos

        payload_state = torch.cat(
            [
                self.target_payload_rpos,  # 3
                self.payload_rot,  # 4
                payload_vels,  # 6
                self.payload_heading,  # 3
                self.payload_up, # 3
            ],
            dim=-1,
        ).unsqueeze(1)

        obstacle_payload_rpos = self.obstacle_pos[..., [0, 2]] - self.payload_pos[..., [0, 2]].unsqueeze(1)

        obs = TensorDict({}, [self.num_envs, self.drone.n])
        obs["self"] = torch.cat(
            [-payload_drone_rpos, self.drone_states[..., 3:]], dim=-1
        ).unsqueeze(2) # [..., 1, state_dim]
        obs["others"] = torch.cat(
            [self.drone_rpos, self.drone_pdist, vmap(others)(self.drone_states[..., 3:])], dim=-1
        ) # [..., n-1, state_dim + 1]
        obs["payload"] = payload_state.expand(-1, self.drone.n, -1).unsqueeze(2) # [..., 1, 22]
        obs["obstacles"] = obstacle_payload_rpos.unsqueeze(1).expand(-1, self.drone.n, 2, 2)

        state = TensorDict({}, self.num_envs)
        state["payload"] = payload_state # [..., 1, 22]
        state["drones"] = obs["self"].squeeze(2) # [..., n, state_dim]
        state["obstacles"] = obstacle_payload_rpos # [..., 2, 2]

        payload_pos_error = torch.norm(self.target_payload_rpos, dim=-1, keepdim=True)
    
        self.stats["payload_pos_error"].mul_(self.alpha).add_((1-self.alpha) * payload_pos_error)

        return TensorDict({
            "drone.obs": obs, 
            "drone.state": state,
            "info": self.info,
            "stats": self.stats
        }, self.num_envs).apply(lambda x: torch.nan_to_num(x, 0))

    def _compute_reward_and_done(self):
        joint_positions = (
            self.group.get_joint_positions()[..., :16]
            / self.group.joint_limits[..., :16]
        )
        
        distance = torch.norm(self.target_payload_rpos, dim=-1, keepdim=True)
        separation = self.drone_pdist.min(dim=-2).values.min(dim=-2).values

        reward = torch.zeros(self.num_envs, self.drone.n, 1, device=self.device)
        reward_pose = 1 / (1 + torch.square(distance * self.reward_distance_scale))
        # reward_pose = torch.exp(-distance * self.reward_distance_scale)
        reward_up = torch.square((self.payload_up[:, 2] + 1) / 2).unsqueeze(-1)

        reward_effort = self.reward_effort_weight * torch.exp(-self.effort).mean(-1, keepdim=True)
        reward_separation = torch.square(separation / self.safe_distance).clamp(0, 1)
        reward_joint_limit = 0.5 * torch.mean(1 - torch.square(joint_positions), dim=-1)

        collision = (
            self.obstacles
            .get_net_contact_forces()
            .any(-1)
            .any(-1, keepdim=True)
        )
        collision_reward = collision.float()
        self.stats["collision"] += collision_reward

        reward[:] = (
            reward_separation * (
                reward_pose 
                + reward_pose * reward_up 
                + reward_joint_limit
                + reward_effort
            ) * (1 - collision_reward)
        ).unsqueeze(1)

        done_misbehave = (self.drone_states[..., 2] < 0.2) | (self.drone_states[..., 2] > 3.)
        
        done = (
            (self.progress_buf >= self.max_episode_length).unsqueeze(-1) 
            | done_misbehave.any(-1, keepdim=True)
            | (self.payload_pos[:, 2] < 0.3).unsqueeze(1)
        )

        if self.reset_on_collision:
            done = done | collision

        self._tensordict["return"] += reward
        return TensorDict(
            {
                "reward": {"drone.reward": reward},
                "return": self._tensordict["return"],
                "done": done,
            },
            self.batch_size,
        )
