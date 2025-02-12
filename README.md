# Multi-UAV Pursuit-Evasion with Online Planning in Unknown Environments by Deep Reinforcement Learning 

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Jiayu Chen, Chao Yu, Guosheng Li, Wenhao Tang, Xinyi Yang, Botian Xu, Huazhong Yang, Yu Wang

Website: https://sites.google.com/view/pursuit-evasion-rl

The implementation in this repositorory is used in the paper "Multi-UAV Pursuit-Evasion with Online Planning in Unknown Environments by Deep Reinforcement Learning". This repository is heavily based on https://github.com/btx0424/OmniDrones.git.

## Approach

<div align=center>
<img src="https://github.com/jiayu-ch15/Multi-UAV-pursuit-evasion/blob/main/figures/overview.png" width="700"/>
</div>

Pipeline of our method. We begin by calibrating the parameters of the quadrotor dynamics model through system identification. Next, we introduce an Adaptive Environment Generator to automatically generate tasks for policy training and employ an Evader Prediction-Enhanced Network for efficient capture. Finally, with two-stage reward refinement, the learned policy is directly transferred to real quadrotors in a zero-shot manner.

## Install

#### 1. Download Isaac Sim (local version)

Download the [Omniverse Isaac Sim (local version)](https://developer.nvidia.com/isaac-sim) and install the desired Isaac Sim release **(version 2022.2.0)** following the [official document](https://docs.omniverse.nvidia.com/isaacsim/latest/installation/install_workstation.html). *Note that Omniverse Isaac Sim supports multi-user access, eliminating the need for repeated downloads and installations across different user accounts.*

Set the following environment variables to your ``~/.bashrc`` or ``~/.zshrc`` files :

```
# Isaac Sim root directory
export ISAACSIM_PATH="${HOME}/.local/share/ov/pkg/isaac_sim-2022.2.0"
```

*(Currently we use isaac_sim-2022.2.0. Whether other versions can work or not is not guaranteed.)*

After adding the environment variable, apply the changes by running:
```
source ~/.bashrc
```

#### 2. Conda

Although Isaac Sim comes with a built-in Python environment, we recommend using a seperate conda environment which is more flexible. We provide scripts to automate environment setup when activating/deactivating a conda environment at ``Multi-UAV-pursuit-evasion/conda_setup``.

```
conda create -n sim python=3.7
conda activate sim

# at Multi-UAV-pursuit-evasion/
cp -r conda_setup/etc $CONDA_PREFIX
# re-activate the environment
conda activate sim
# install Multi-UAV-pursuit-evasion
pip install -e .

# verification
python -c "from omni.isaac.kit import SimulationApp"
# which torch is being used
python -c "import torch; print(torch.__path__)"
```

#### 3. Third Party Packages
Multi-UAV-pursuit-evasion requires specific versions of the `tensordict` and `torchrl` packages. For the ``deploy`` branch, it supports `tensordict` version 0.1.2+5e6205c and `torchrl` version 0.1.1+e39e701. 

We manage these two packages using Git submodules to ensure that the correct versions are used. To initialize and update the submodules, follow these steps:

Get the submodules:
```
# at Multi-UAV-pursuit-evasion/
git submodule update --init --recursive
```
Pip install these two packages respectively:
```
# at Multi-UAV-pursuit-evasion/
cd third_party/tensordict
pip install -e .
```
```
# at Multi-UAV-pursuit-evasion/
cd third_party/torchrl
pip install -e .
```
#### 4. Verification
```
# at Multi-UAV-pursuit-evasion/
cd scripts
python train.py headless=true wandb.mode=disabled total_frames=50000 task=Hover
```

#### 5. Working with VSCode

To enable features like linting and auto-completion with VSCode Python Extension, we need to let the extension recognize the extra paths we added during the setup process.

Create a file ``.vscode/settings.json`` at your workspace if it is not already there.

After activating the conda environment, run

```
printenv > .vscode/.python.env
``````

and edit ``.vscode/settings.json`` as:

```
{
    // ...
    "python.envFile": "${workspaceFolder}/.vscode/.python.env",
}
```

## Usage

For usage and more details, please refer to the [documentation](https://omnidrones.readthedocs.io/en/latest/).

The code is organized as follow:
```
cfg
|-- train.yaml
|-- algo
    |-- mappo.yaml
|-- task
    |-- HideAndSeek_envgen.yaml
    |-- HideAndSeek.yaml
    |-- Hover.yaml
omni_drones
|-- envs
    |-- hide_and_seek
        |-- hideandseek_envgen.py
        |-- hideandseek.py
    |-- single
        |-- hover.py
scripts
|-- train.py
|-- train.deploy.py
|-- train_generator.py
```

```
# at Multi-UAV-pursuit-evasion/
cd scripts
# Train the pursuit-evasion task with Automatic Environment Generator.
python train_generator.py
# Train the pursuit-evasion task without Automatic Environment Generator.
python train.py
# In the first stage, we get the best model, checkpoint.pt.
```

```
# at Multi-UAV-pursuit-evasion/
cd scripts
# use_random_cylinder = 0, scenario_flag = 'wall' in HideAndSeek.yaml
# four evaluation scenarios: # 'wall', 'narrow_gap', 'random', 'passage'
# evaluate the policy and obtain a video
python eval.py
```

<div align=center>
<img src="https://github.com/jiayu-ch15/Multi-UAV-pursuit-evasion/blob/main/figures/evaluation.png" width="700"/>
</div>

## Real-world deployment
We deploy the policy on three real CrazyFlie 2.1 quadrotors. The key parameters of dynamics model is listed as follow:
```
# in crazyflie.yaml
mass: 0.0321
inertia:
  xx: 1.4e-5
  xy: 0.0
  xz: 0.0
  yy: 1.4e-5
  yz: 0.0
  zz: 2.17e-5
force_constants: 2.350347298350041e-08
max_rotation_velocities: 2315
moment_constants: 7.24e-10
time_constant: 0.025
```

We fine-tune the policy with two-stage reward refinement.
```
# in train.yaml, setup the path of the best checkpoint obtained in the first stage
# model_dir: /absolute/path/checkpoint.pt
python train_deploy.py
```

## Citation

Please cite [this paper](http://arxiv.org/abs/2409.15866
) if you use our method in your work:

```
@misc{chen2024multiuavpursuitevasiononlineplanning,
      title={Multi-UAV Pursuit-Evasion with Online Planning in Unknown Environments by Deep Reinforcement Learning}, 
      author={Jiayu Chen and Chao Yu and Guosheng Li and Wenhao Tang and Xinyi Yang and Botian Xu and Huazhong Yang and Yu Wang},
      year={2024},
      eprint={2409.15866},
      archivePrefix={arXiv},
      primaryClass={cs.RO},
      url={https://arxiv.org/abs/2409.15866}, 
}
```
