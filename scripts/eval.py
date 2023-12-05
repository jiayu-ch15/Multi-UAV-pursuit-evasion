import logging
import os
import time

import hydra
from tensordict.tensordict import TensorDictBase
import torch
from torchrl.data.tensor_specs import TensorSpec
import numpy as np
import wandb
from functorch import vmap
from omegaconf import OmegaConf

from omni_drones import CONFIG_PATH, init_simulation_app
from omni_drones.utils.torchrl import SyncDataCollector, AgentSpec
from omni_drones.utils.torchrl.transforms import (
    DepthImageNorm,
    LogOnEpisode, 
    FromMultiDiscreteAction, 
    FromDiscreteAction,
    ravel_composite,
    VelController,
    History
)
from omni_drones.utils.wandb import init_wandb
from omni_drones.learning import (
    MAPPOPolicy, 
    # HAPPOPolicy,
    # QMIXPolicy,
    # DQNPolicy,
    # SACPolicy,
    # TD3Policy,
    # MATD3Policy,
    # DreamerPolicy,
    # TDMPCPolicy
)

from setproctitle import setproctitle
from tensordict import TensorDict
from torchrl.envs.transforms import (
    TransformedEnv, 
    InitTracker, 
    Compose,
    CatTensors,
    StepCounter,
)

from tqdm import tqdm

class Every:
    def __init__(self, func, steps):
        self.func = func
        self.steps = steps
        self.i = 0

    def __call__(self, *args, **kwargs):
        if self.i % self.steps == 0:
            self.func(*args, **kwargs)
        self.i += 1

@hydra.main(version_base=None, config_path=CONFIG_PATH, config_name="eval")
def main(cfg):
    OmegaConf.register_new_resolver("eval", eval)
    OmegaConf.resolve(cfg)
    OmegaConf.set_struct(cfg, False)
    run = init_wandb(cfg)
    simulation_app = init_simulation_app(cfg)
    setproctitle(run.name)
    print(OmegaConf.to_yaml(cfg))

    from omni_drones.envs.isaac_env import IsaacEnv
    from omni_drones.sensors.camera import Camera, PinholeCameraCfg
    algos = {
        "mappo": MAPPOPolicy, 
        # "happo": HAPPOPolicy,
        # "qmix": QMIXPolicy,
        # "dqn": DQNPolicy,
        # "sac": SACPolicy,
        # "td3": TD3Policy,
        # "matd3": MATD3Policy,
        # "dreamer": DreamerPolicy,
        # "tdmpc": TDMPCPolicy
    }

    env_class = IsaacEnv.REGISTRY[run.config]
    base_env = env_class(cfg, headless=cfg.headless)
    
    def log(info):
        print(OmegaConf.to_yaml(info))
        # run.log(info)

    stats_keys = [
        k for k in base_env.observation_spec.keys(True, True) 
        if isinstance(k, tuple) and k[0]=="stats"
    ]
    logger = LogOnEpisode(
        cfg.env.num_envs,
        in_keys=["return", "return", "progress", *stats_keys],
        log_keys=["return", "return_std", "ep_length", *stats_keys],
        logger_func=log,
        process_func={"return_std": lambda x: torch.std(x).item()}
    )
    transforms = [InitTracker(), logger]

    # a CompositeSpec is by deafault processed by a entity-based encoder
    # flatten it to use a MLP encoder instead
    if cfg.task.get("flatten_obs", False):
        transforms.append(ravel_composite(base_env.observation_spec, ("agents", "observation")))
    if cfg.task.get("flatten_state", False):
        transforms.append(ravel_composite(base_env.observation_spec, "state"))
    if (
        cfg.task.get("flatten_intrinsics", True)
        and ("agents", "intrinsics") in base_env.observation_spec.keys(True)
    ):
        transforms.append(ravel_composite(base_env.observation_spec, ("agents", "intrinsics"), start_dim=-1))

    if cfg.task.get("history", False):
        transforms.append(History([("agents", "observation")]))
    
    if cfg.task.get("visual_obs", False):
        min_depth = cfg.task.camera.get("min_depth", 0.1)
        max_depth = cfg.task.camera.get("max_depth", 5)
        transforms.append(
            DepthImageNorm([("drone.obs", "distance_to_camera")], 
                            min_range=min_depth, max_range=max_depth)
        )
    
    # optionally discretize the action space or use a controller
    action_transform: str = cfg.task.get("action_transform", None)
    if action_transform is not None:
        if action_transform.startswith("multidiscrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromMultiDiscreteAction(("action", "drone.action"), nbins=nbins)
            transforms.append(transform)
        elif action_transform.startswith("discrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromDiscreteAction(("action", "drone.action"), nbins=nbins)
            transforms.append(transform)
        elif action_transform == "controller":
            controller_cls = base_env.drone.DEFAULT_CONTROLLER
            print(f"Use controller {controller_cls}")
            controller = controller_cls(
                base_env.dt, 
                9.81, 
                base_env.drone.params
            ).to(base_env.device)
            transform = VelController(vmap(vmap(controller)), ("action", "drone.action"))
            transforms.append(transform)
        elif not action_transform.lower() == "none":
            raise NotImplementedError(f"Unknown action transform: {action_transform}")
    
    env = TransformedEnv(base_env, Compose(*transforms)).train()
    env.set_seed(cfg.seed)

    camera_cfg = PinholeCameraCfg(
        sensor_tick=0,
        resolution=(960, 720),
        data_types=["rgb"],
        usd_params=PinholeCameraCfg.UsdCameraCfg(
            focal_length=24.0,
            focus_distance=400.0,
            horizontal_aperture=20.955,
            clipping_range=(0.1, 1.0e5),
        ),
    )
    
    camera = Camera(camera_cfg)
    camera.spawn(["/World/Camera"], translations=[(7.5, 7.5, 7.5)], targets=[(0, 0, 0.5)])
    camera.initialize("/World/Camera")

    agent_spec = env.agent_spec["drone"]
    policy = algos[cfg.algo.name.lower()](
        cfg.algo, agent_spec=agent_spec, device="cuda"
    )

    ckpt_name = "/home/jingyihuang/isaac_ws/OmniDrones/scripts/wandb/offline-run-20231107_233000-ckcf61ne/files/checkpoint_final.pt"
    # ckpt = wandb.restore(ckpt_name, run.path)
    state_dict = torch.load(ckpt)
    policy.load_state_dict(state_dict)

    @torch.no_grad()
    def evaluate(
        seed: int = 0,
    ):
        frames = []

        base_env.enable_render(True)
        base_env.eval()
        env.eval()
        env.set_seed(seed)

        from tqdm import tqdm
        t = tqdm(total=base_env.max_episode_length)
        
        def record_frame(*args, **kwargs):
            frame = env.base_env.render(mode="rgb_array")
            frames.append(frame)
            t.update(2)

        trajs = env.rollout(
            max_steps=base_env.max_episode_length,
            policy=lambda x: policy(x, deterministic=True),
            callback=Every(record_frame, 2),
            auto_reset=True,
            break_when_any_done=False,
            return_contiguous=False
        ).clone()

        base_env.enable_render(not cfg.headless)
        env.reset()

        done = trajs.get(("next", "done"))
        first_done = torch.argmax(done.long(), dim=1).cpu()

        def take_first_episode(tensor: torch.Tensor):
            indices = first_done.reshape(first_done.shape+(1,)*(tensor.ndim-2))
            return torch.take_along_dim(tensor, indices, dim=1).reshape(-1)

        traj_stats = {
            k: take_first_episode(v)
            for k, v in trajs[("next", "stats")].cpu().items()
        }

        info = {
            "eval/stats." + k: torch.mean(v.float()).item() 
            for k, v in traj_stats.items()
        }

        if len(frames):
            video_array = np.stack(frames).transpose(0, 3, 1, 2)
            frames.clear()
            info["recording"] = wandb.Video(
                video_array, fps=0.5 / cfg.sim.dt, format="mp4"
            )
        frames.clear()
        return info

    info = evaluate()
    run.log(info)
    
    simulation_app.close()


if __name__ == "__main__":
    main()
