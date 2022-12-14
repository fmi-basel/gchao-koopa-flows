from typing import List
import logging
import os
import subprocess

from prefect import flow
from prefect import get_run_logger
from prefect_dask import DaskTaskRunner
import koopa

import tasks_postprocess
import tasks_preprocess
import tasks_segment
import tasks_spots
import tasks_util


def file_independent(config: dict):
    """Wrapper for preliminary, file independent tasks."""
    tasks_util.save_conda_env.submit(path_out=config["output_path"])
    tasks_util.save_system_information.submit(path_out=config["output_path"])

    if not config["alignment_enabled"]:
        return None

    tasks_preprocess.align.submit(
        path_in=config["alignment_path"], path_out=config["output_path"], config=config
    ).wait()


def cell_segmentation(
    fname: str,
    config: dict,
    kwargs: dict,
    dependencies: list,
    gpu: bool,
):
    """Wrapper for cell segmentation task submission."""
    if not config["brains_enabled"]:
        if config["selection"] == "both":
            return tasks_segment.segment_cells_both.submit(
                fname, **kwargs, wait_for=dependencies
            )
        else:
            return tasks_segment.segment_cells_single.submit(
                fname, **kwargs, wait_for=dependencies
            )

    brain_1 = tasks_segment.segment_cells_predict.submit(
        fname, **kwargs, gpu=gpu, wait_for=dependencies
    )
    brain_2 = tasks_segment.segment_cells_merge.submit(
        fname, **kwargs, wait_for=[brain_1]
    )
    return tasks_segment.dilate_cells.submit(fname, **kwargs, wait_for=[brain_2])


def other_segmentation(
    fname: str, config: dict, kwargs: dict, dependencies: list, gpu: bool
) -> list:
    """Wrapper for segment_other task submission."""
    if not config["sego_enabled"]:
        return []

    channels = range(len(config["sego_channels"]))
    tasks = []
    for index in channels:
        tasks.append(
            tasks_segment.segment_other.submit(
                fname, **kwargs, gpu=gpu, index_list=index, wait_for=dependencies
            )
        )
    return tasks


def spot_detection(
    fname: str, config: dict, kwargs: dict, dependencies: list, gpu: bool
) -> list:
    """Wrapper for spot detection and tracking task submission."""
    # Detection
    channels = range(len(config["detect_channels"]))
    detect = []
    for index in channels:
        detect.append(
            tasks_spots.detect.submit(
                fname, **kwargs, gpu=gpu, index_list=index, wait_for=dependencies
            )
        )
    if not config["do_3d"] and not config["do_timeseries"]:
        return detect

    # Tracking
    track = []
    for index in config["detect_channels"]:
        track.append(
            tasks_spots.track.submit(
                fname, **kwargs, index_channel=index, wait_for=detect
            )
        )
    return track


def colocalization(fname: str, config: dict, kwargs: dict, dependencies: list) -> list:
    """Wrapper for spot colocalization task submission."""
    if not config["coloc_enabled"]:
        return []

    colocalization = []
    for reference, transform in config["coloc_channels"]:
        if config["do_timeseries"]:
            colocalization.append(
                tasks_spots.colocalize_track.submit(
                    fname,
                    **kwargs,
                    index_reference=reference,
                    index_transform=transform,
                    wait_for=dependencies,
                )
            )
        else:
            colocalization.append(
                tasks_spots.colocalize_frame.submit(
                    fname,
                    **kwargs,
                    index_reference=reference,
                    index_transform=transform,
                    wait_for=dependencies,
                )
            )
    return colocalization


def merging(fnames: List[str], config: dict, kwargs: dict, dependencies: list) -> None:
    """Wrapper for merginc and summary file creation."""
    singles = []
    for fname, dependency in zip(fnames, dependencies):
        singles.append(
            tasks_postprocess.merge_single.submit(fname, **kwargs, wait_for=dependency)
        )
    tasks_postprocess.merge_all.submit(config["output_path"], singles, wait_for=singles)


@flow(name="Update Koopa")
def update_koopa(reinstall: bool = False):
    """Updates koopa via pip after checking for the latest version.

    Arguments:
        * reinstall: forces reinstall even if already on latest version.
    """
    logger = get_run_logger()
    logger.info("Starting koopa's update")
    subprocess.check_call(["pip", "install", "koopa"])

    def __get_version():
        data = subprocess.check_output(["pip", "show", "koopa"]).decode()
        if "WARNING" in data:
            return "0.0.0/Not-installed"
        return data.split("\n")[1].split(" ")[1]

    arguments = ["--upgrade", "--no-cache-dir"]
    if reinstall:
        arguments.extend(["--force", "--ignore-installed"])

    pre = __get_version()
    subprocess.check_call(["python", "-m", "pip", "install", *arguments, "koopa"])
    post = __get_version()
    logger.info(f"Upgraded koopa from Version {pre} -> {post}")


def core_workflow(
    config_path: str, force: bool, logger: logging.Logger, gpu: bool = False
):
    # File independent tasks
    config = tasks_util.configuration(config_path, force)
    file_independent(config)

    # Workflow
    fnames = koopa.util.get_file_list(config["input_path"], config["file_ext"])
    logger.info(f"Running analysis with {len(fnames)} files - {fnames}")
    kwargs = dict(path=config["output_path"], config=config)

    dependencies = []
    for fname in fnames:
        preprocess = [tasks_preprocess.preprocess.submit(fname, **kwargs)]
        seg_cells = cell_segmentation(
            fname, config, kwargs, dependencies=preprocess, gpu=gpu
        )
        seg_other = other_segmentation(
            fname, config, kwargs, dependencies=preprocess, gpu=gpu
        )
        spots = spot_detection(fname, config, kwargs, dependencies=preprocess, gpu=gpu)
        coloc = colocalization(fname, config, kwargs, dependencies=spots)
        dependencies.append((*spots, *coloc, seg_cells, *seg_other))

    merging(fnames, config, kwargs, dependencies=dependencies)


@flow(
    name="Koopa",
    version=koopa.__version__,
    task_runner=DaskTaskRunner(
        cluster_class="dask_jobqueue.SLURMCluster",
        cluster_kwargs={
            "account": "dlthings",
            "queue": "cpu_short",
            "cores": 16,
            "processes": 1,
            "memory": "64 GB",
            "walltime": "02:00:00",
            "job_extra_directives": [
                "--ntasks=1",
                "--output=/tungstenfs/scratch/gmicro_share/_prefect/slurm/output/%j.out",
            ],
            "worker_extra_args": [
                "--lifetime",
                "240m",
                "--lifetime-stagger",
                "15m",
            ],
            "job_script_prologue": [
                "conda run -p /tungstenfs/scratch/gmicro_share/_prefect/miniconda3/envs/airtable python tungstenfs/scratch/gmicro_share/_prefect/airtable/log-slurm-job.py --config /tungstenfs/scratch/gmicro/_prefect/airtable/slurm-job-log.ini"
            ],
        },
        adapt_kwargs={
            "minimum": 1,
            "maximum": 8,
        },
    ),
)
def workflow(
    config_path: str,
    force: bool = False,
    root_dir: str = "/tungstenfs/scratch/gmicro_prefect/gchao",
):
    """Core koopa workflow.

    Arguments:
        * config_path: Path to koopa configuration file.
            Path must be passed linux-compatible (e.g. /tungstenfs/scratch/...).
            The default configuration file can be viewed and downloaded
            [here](https://raw.githubusercontent.com/fmi-basel/gchao-koopa-flows/main/koopa.cfg).

        * force: If selected, the entire workflow will be re-run overwriting previous files.
            Otherwise, only the not yet executed components (missing files) are run without overwriting.

    All documentation can be found on the koopa wiki (https://github.com/fmi-basel/gchao-koopa/wiki).
    """
    logger = get_run_logger()
    logger.info("Started running Koopa!")
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = os.path.join(root_dir, ".cellpose")
    core_workflow(config_path=config_path, force=force, logger=logger, gpu=False)


@flow(
    name="Koopa-GPU",
    version=koopa.__version__,
    task_runner=DaskTaskRunner(
        cluster_class="dask_jobqueue.SLURMCluster",
        cluster_kwargs={
            "account": "dlthings",
            "queue": "gpu_short",
            "cores": 8,
            "processes": 1,
            "memory": "32 GB",
            "walltime": "04:00:00",
            "job_extra_directives": [
                "--gpus-per-node=1",
                "--ntasks=1",
                "--output=/tungstenfs/scratch/gmicro_share/_prefect/slurm/output/%j.out",
            ],
            "worker_extra_args": [
                "--lifetime",
                "240m",
                "--lifetime-stagger",
                "15m",
            ],
            "job_script_prologue": [
                "conda run -p /tungstenfs/scratch/gmicro_share/_prefect/miniconda3/envs/airtable python tungstenfs/scratch/gmicro_share/_prefect/airtable/log-slurm-job.py --config /tungstenfs/scratch/gmicro/_prefect/airtable/slurm-job-log.ini"
            ],
        },
        adapt_kwargs={
            "minimum": 1,
            "maximum": 4,
        },
    ),
)
def gpu_workflow(
    config_path: str,
    force: bool = False,
    root_dir: str = "/tungstenfs/scratch/gmicro_prefect/gchao",
):
    """GPU specific workflow."""
    logger = get_run_logger()
    logger.info("Started running Koopa-GPU!")
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = os.path.join(root_dir, ".cellpose")
    core_workflow(config_path=config_path, force=force, logger=logger, gpu=True)
