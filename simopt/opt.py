import os

from typing import Dict, Optional
import torch
from functorch import vmap
import torch.optim as optim
from scipy import optimize
import time

import hydra
from omegaconf import OmegaConf
from omni_drones import init_simulation_app
from tensordict import TensorDict
import pandas as pd
import pdb
import numpy as np
import yaml
from skopt import Optimizer
from omni_drones.utils.torch import quat_rotate, quat_rotate_inverse
import matplotlib.pyplot as plt
import numpy as np
from simopt.core import RealWorldDataBuffer, DataBufferBase

rosbags = [
    '/home/jiayu/OmniDrones/simopt/real_data/rl_hover_1.csv',
    # '/home/jiayu/OmniDrones/simopt/real_data/size0_8.csv',
    # '/home/jiayu/OmniDrones/simopt/real_data/size1_0.csv',
    # '/home/jiayu/OmniDrones/simopt/real_data/size1_2.csv',
]
# shape [-1, 37]
data_path = '/home/jiayu/OmniDrones/simopt/real_data'

@hydra.main(version_base=None, config_path=".", config_name="real2sim")
def main(cfg):
    """
        preprocess real data
        real_data: [batch_size, T, dimension]
    """

    real_data = []

    df = RealWorldDataBuffer(data_path)
    
    observations = df.observations
    actions = df.actions
    
    num_envs = observations.shape[0]

    # start sim
    OmegaConf.resolve(cfg)
    simulation_app = init_simulation_app(cfg)
    import omni_drones.utils.scene as scene_utils
    from omni.isaac.core.simulation_context import SimulationContext
    from omni_drones.controllers import RateController, PIDRateController
    from omni_drones.robots.drone import MultirotorBase
    from omni_drones.utils.torch import euler_to_quaternion, quaternion_to_euler
    from omni_drones.sensors.camera import Camera, PinholeCameraCfg
    from omni.isaac.cloner import GridCloner
    from omni.isaac.core.utils import prims as prim_utils, stage as stage_utils
    import omni_drones.utils.kit as kit_utils

    dt = 0.01
    g = 9.81
    sim = SimulationContext(
        stage_units_in_meters=1.0,
        physics_dt=dt,
        rendering_dt=dt,
        sim_params=cfg.sim,
        backend="torch",
        device=cfg.sim.device,
    )

    # create cloner for duplicating the scenes
    env_ns = "/World/envs"
    template_env_ns = "/World/envs/env_0"
    cloner = GridCloner(spacing=8)
    cloner.define_base_env("/World/envs")
    # create the xform prim to hold the template environment
    if not prim_utils.is_prim_path_valid(template_env_ns):
        prim_utils.define_prim(template_env_ns)
    # setup single scene
    # scene_utils.design_scene()
    drone_model = MultirotorBase.REGISTRY['crazyflie']
    cfg = drone_model.cfg_cls(force_sensor=False)
    drone: MultirotorBase = drone_model(cfg=cfg)
    kit_utils.create_ground_plane(
        "/World/defaultGroundPlane",
        static_friction=1.0,
        dynamic_friction=1.0,
        restitution=0.0,
    )
    drone.spawn(translations=[(0.0, 0.0, 1.5)])
    global_prim_paths =  ["/World/defaultGroundPlane"] # # global_prim_paths = _design_scene()
    # check if any global prim paths are defined
    if global_prim_paths is None:
        global_prim_paths = list()
    envs_prim_paths = cloner.generate_paths(
        env_ns + "/env", num_envs
    )
    envs_positions = cloner.clone(
        source_prim_path=template_env_ns,
        prim_paths=envs_prim_paths,
        replicate_physics=False,
    )
    # convert environment positions to torch tensor
    envs_positions = torch.tensor(
        envs_positions, dtype=torch.float, device='cuda:0'
    )

    # filter collisions within each environment instance
    physics_scene_path = sim.get_physics_context().prim_path
    cloner.filter_collisions(
        physics_scene_path,
        "/World/collisions",
        prim_paths=envs_prim_paths,
        global_paths=global_prim_paths,
    )
    
    def set_drone_state(pos, quat, vel, ang_vel):
        pos = pos.to(device=sim.device).float()
        quat = quat.to(device=sim.device).float()
        vel = vel.to(device=sim.device).float()
        ang_vel = ang_vel.to(device=sim.device).float()
        drone.set_world_poses(pos + envs_positions, quat)
        whole_vel = torch.cat([vel, ang_vel], dim=-1)
        drone.set_velocities(whole_vel)
        # flush the buffer so that the next getter invocation 
        # returns up-to-date values
        sim._physics_sim_view.flush() 
    
    def evaluate(params, real_data):
        """
            evaluate in Omnidrones
            params: suggested params
            real_data: [batch_size, T, dimension]
            sim: omnidrones core
            drone: crazyflie
            controller: the predefined controller
        """
        tunable_parameters = {
            'mass': params[0],
            'inertia_xx': params[1],
            'inertia_yy': params[2],
            'inertia_zz': params[3],
            'arm_lengths': params[4],
            'force_constants': params[5],
            'max_rotation_velocities': params[6],
            'moment_constants': params[7],
            'drag_coef': params[8],
            'time_constant': params[9],
            # 'gain': params[10:]
            'pid_kp': params[10:13],
            'pid_kd': params[13:15],
            'pid_ki': params[15:18],
            'iLimit': params[18:21],
        }
        
        # reset sim
        sim.reset()
        drone.initialize_byTunablePara(tunable_parameters=tunable_parameters)
        controller = PIDRateController(dt, g, drone.params).to(sim.device)
        controller = controller.to(sim.device)
        
        # shuffle index and split into batches
        shuffled_idx = torch.randperm(observations.shape[0])
        shuffled_obs = observations[shuffled_idx]
        shuffled_actions = actions[shuffled_idx]
        
        # update simulation parameters
        """
            1. set parameters into sim
            2. update parameters
            3. export para to yaml 
        """
        sim_pos_list = []
        real_pos_list = []
        
        sim_quat_list = []
        real_quat_list = []

        sim_vel_list = []
        real_vel_list = []

        sim_ang_vel_list = []
        real_ang_vel_list = []
        
        for i in range(shuffled_obs.shape[1] - 1):
            real_pos = torch.tensor(shuffled_obs[:, i, :3])
            real_vel = torch.tensor(shuffled_obs[:, i, 3:6])
            real_quat = torch.tensor(shuffled_obs[:, i, 6:10])
            real_ang_vel = torch.tensor(shuffled_obs[:, i, 10:13])
            if i == 0:
                set_drone_state(real_pos, real_quat, real_vel, real_ang_vel)
            
            real_action = torch.tensor(shuffled_actions[:, i]).to(sim.device)
            drone.apply_action(real_action.unsqueeze(1))

            sim.step(render=True)
            
            if sim.is_stopped():
                break
            if not sim.is_playing():
                sim.render()
                continue

            # get simulated drone state
            next_sim_state = drone.get_state().squeeze(1).cpu()
            next_sim_pos = next_sim_state[..., :3] - envs_positions.cpu() # get env pos
            next_sim_quat = next_sim_state[..., 3:7]
            next_sim_vel = next_sim_state[..., 7:10]
            next_sim_ang_vel = next_sim_state[..., 10:13]

            # next real states, ground truth
            next_real_pos = torch.tensor(shuffled_obs[:, i+1, :3])
            next_real_vel = torch.tensor(shuffled_obs[:, i+1, 3:6])
            next_real_quat = torch.tensor(shuffled_obs[:, i+1, 6:10])
            next_real_ang_vel = torch.tensor(shuffled_obs[:, i+1, 10:13])
            
            sim_pos_list.append(next_sim_pos.cpu().detach().numpy())
            sim_quat_list.append(next_sim_quat.cpu().detach().numpy())
            sim_vel_list.append(next_sim_vel.cpu().detach().numpy())
            sim_ang_vel_list.append(next_sim_ang_vel.cpu().detach().numpy())
            
            real_pos_list.append(next_real_pos.cpu().detach().numpy())
            real_quat_list.append(next_real_quat.cpu().detach().numpy())
            real_vel_list.append(next_real_vel.cpu().detach().numpy())
            real_ang_vel_list.append(next_real_ang_vel.cpu().detach().numpy())
                
        sim_pos_list = np.array(sim_pos_list)
        sim_quat_list = np.array(sim_quat_list)
        sim_vel_list = np.array(sim_vel_list)
        sim_ang_vel_list = np.array(sim_ang_vel_list)

        real_pos_list = np.array(real_pos_list)
        real_quat_list = np.array(real_quat_list)
        real_vel_list = np.array(real_vel_list)
        real_ang_vel_list = np.array(real_ang_vel_list)
        
        # # normalization
        # min_pos_list = np.min(np.min(sim_pos_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # max_pos_list = np.max(np.max(sim_pos_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # sim_pos_list = (sim_pos_list - min_pos_list) / (max_pos_list - min_pos_list)
        # real_pos_list = (real_pos_list - min_pos_list) / (max_pos_list - min_pos_list)
        # min_vel_list = np.min(np.min(sim_vel_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # max_vel_list = np.max(np.max(sim_vel_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # sim_vel_list = (sim_vel_list - min_vel_list) / (max_vel_list - min_vel_list)
        # real_vel_list = (real_vel_list - min_vel_list) / (max_vel_list - min_vel_list)
        # min_quat_list = np.min(np.min(sim_quat_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # max_quat_list = np.max(np.max(sim_quat_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # sim_quat_list = (sim_quat_list - min_quat_list) / (max_quat_list - min_quat_list)
        # real_quat_list = (real_quat_list - min_quat_list) / (max_quat_list - min_quat_list)
        # min_ang_vel_list = np.min(np.min(sim_ang_vel_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # max_ang_vel_list = np.max(np.max(sim_ang_vel_list, axis=0), axis=0)[np.newaxis, np.newaxis, :]
        # sim_ang_vel_list = (sim_ang_vel_list - min_ang_vel_list) / (max_ang_vel_list - min_ang_vel_list)
        # real_ang_vel_list = (real_ang_vel_list - min_ang_vel_list) / (max_ang_vel_list - min_ang_vel_list)
        
        # debug
        error_rpy = sim_quat_list - real_quat_list
        error_rpy_dot = sim_ang_vel_list - real_ang_vel_list
        error_xyz = (sim_pos_list - real_pos_list)
        error_xyz_dot = (sim_vel_list - real_vel_list)
        
        error = np.concatenate([error_rpy, error_rpy_dot, error_xyz, error_xyz_dot], axis=-1)
        # error = np.concatenate([error_rpy_dot], axis=-1)
        
        L1_loss = np.linalg.norm(error, axis=-1, ord=1)
        L2_loss = np.linalg.norm(error, axis=-1, ord=2)
        L = np.mean(L1_loss + L2_loss, axis=-1)
        
        loss = torch.tensor(0.0, dtype=torch.float)
        gamma = 0.95 # discounted factor
        for i in range(shuffled_obs.shape[1] - 1):
            loss += L[i] * gamma**i
        return loss 

    # PID
    params = [
        0.0321, 1.4e-5, 1.4e-5, 2.17e-5, 0.043,
        2.350347298350041e-08, 2315, 7.24e-10, 0.2,
        0.025, # Tm
        # controller
        250.0, 250.0, 120.0, # kp
        2.5, 2.5, 2.5, # kd
        500.0, 500.0, 16.7, # ki
        33.3, 33.3, 166.7 # ilimit
    ]

    """
        'mass': params[0],
        'inertia_xx': params[1],
        'inertia_yy': params[2],
        'inertia_zz': params[3],
        'arm_lengths': params[4],
        'force_constants': params[5], # kf = max_rotation_velocities^2 * force_constants / (1/4mg)
        'max_rotation_velocities': params[6],
        'moment_constants': params[7], # km
        'drag_coef': params[8],
        'time_constant': params[9], # tau
        # 'gain': params[10:]
        'pid_kp': params[10:13],
        'pid_kd': params[13:15],
        'pid_ki': params[15:18],
        'iLimit': params[18:21],
    """
    params_mask = np.array([0] * len(params))

    # update rotor params
    # params_mask[5] = 1
    # params_mask[7] = 1
    params_mask[9] = 1

    params_range = []
    count = 0
    for param, mask in zip(params, params_mask):
        if mask == 1:
            if count == 5: # force_constant -> kf:[1.5, 2.0]
                params_range.append((2.2034505922031636e-08, 2.9379341229375514e-08))
            elif count == 9: # Tm: [0.01, 0.05], v(t+\delta_t) = v(t) * (1 - \delta_t / Tm) + throttle_des * (\delta_t / Tm)
                params_range.append((0.01, 0.03))
        count += 1
    opt = Optimizer(
        dimensions=params_range,
        base_estimator='gp',  # Gaussian Process is a common choice
        n_initial_points=10,   # Number of initial random points to sample
        random_state=0        # Set a random seed for reproducibility
        )

    # set up objective function
    def func(suggested_para, real_data) -> float:
        """A simple callable function that evaluates the objective (fitness)."""
        return evaluate(suggested_para, real_data)

    # now run optimization
    print('*'*55)
    losses = []
    rate_error = []
    epochs = []

    for epoch in range(100):
        print(f'Start with epoch: {epoch}')
        
        x = np.array(opt.ask(), dtype=float)
        # set real params
        set_idx = 0
        for idx, mask in enumerate(params_mask):
            if mask == 1:
                params[idx] = x[set_idx]
                set_idx += 1
        grad = func(params, real_data)
        res = opt.tell(x.tolist(), grad.item())
        
        # TODO: export paras to yaml

        # do the logging and save to disk
        print('Epoch', epoch + 1)
        print(f'CurrentParam/{x.tolist()}')
        print(f'Best/{res.x}')
        print('Best/Loss', res.fun)
        losses.append(grad)
        epochs.append(epoch)
    
    simulation_app.close()

if __name__ == "__main__":
    main()