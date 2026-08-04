# Slurm runtime package installation

- Run ID: 20260804-050357
- Installed Slurm version: 25.11.7-1h100.1
- Source: SchedMD official slurm-25.11.7.tar.bz2, locally built Debian packages
- PluginDir: /usr/lib/x86_64-linux-gnu/slurm
- MUNGE package: 0.5.16-1.1
- MariaDB server package: 1:11.8.6-5ubuntu0.1
- APT transaction: 15 new packages, zero upgraded, zero removed
- Ubuntu Slurm packages mixed: no
- APT version pin: /etc/apt/preferences.d/90-h100-slurm-local
- Package holds: slurm-smd slurm-smd-client slurm-smd-dev slurm-smd-slurmctld slurm-smd-slurmd slurm-smd-slurmdbd
- Services after package installation: stopped and disabled pending configuration
- Deferred check: srun and slurmdbd runtime validation after configuration files exist

Status: SLURM RUNTIME PACKAGES INSTALLED
