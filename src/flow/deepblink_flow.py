import threading
from glob import glob
from os.path import join
from pathlib import Path
from typing import List, Dict, Any, Optional
import os

import tensorflow as tf
from cpr.Serializer import cpr_serializer
from cpr.image.ImageSource import ImageSource
from cpr.utilities.utilities import task_input_hash
import deepblink as pink

import prefect
from koopa.detect import detect_image
from prefect import get_client
from prefect.client.schemas import FlowRun
from prefect.deployments import run_deployment

from .cpr_parquet import ParquetTarget


def exclude_sem_and_model_input_hash(
    context: "TaskRunContext", arguments: Dict[str, Any]
) -> Optional[str]:
    hash_args = {}
    for k, item in arguments.items():
        if (not isinstance(item, threading.Semaphore)) and (
            not isinstance(item, tf.keras.Model)
        ):
            hash_args[k] = item

    return task_input_hash(context, hash_args)

@prefect.task(
    name="deepblink-single-channel",
    cache_result_in_memory=False,
    persist_result=True,
    cache_key_fn=exclude_sem_and_model_input_hash,
)
def deepblink_spot_detection_task(
    image: ImageSource,
        detection_channel: int,
        out_dir: Path,
        model: tf.keras.Model,
        gpu_sem: threading.Semaphore
) -> ParquetTarget:
    logger = prefect.get_run_logger()
    logger.info("running task")

    data = image.get_data()

    try:
        gpu_sem.acquire()
        df = detect_image(
            data, detection_channel, model, refinement_radius=3
        )
    except RuntimeError as e:
        raise e
    finally:
        gpu_sem.release()


    df.insert(loc=0, column="FileID", value=image.get_name())
    logger.info("detected")

    fname_out = os.path.join(
        out_dir, f"test2d_detection_c{detection_channel}", f"{image.get_name()}.parq"
    )
    output = ParquetTarget.from_path(fname_out)
    output.set_data(df)

    logger.info(f"image name - {image.get_name()}")
    logger.info(f"output name - {fname_out}")
    logger.info(f"output path - {output.get_path()}")
    logger.info("finished task")
    return output


@prefect.flow(
    name="deepblink",
    cache_result_in_memory=False,
    persist_result=True,
    result_serializer=cpr_serializer(),
    validate_parameters=False,
)
def deepblink_spot_detection_flow(
    serialized_preprocessed: List[dict],
    out_dir: Path,
    detection_channels: List[int],
    deepblink_models: List[Path],
) -> List[Dict[int, ParquetTarget]]:
    logger = prefect.get_run_logger()
    logger.info("running sub flow")

    preprocessed = [ImageSource(**d) for d in serialized_preprocessed]

    gpu_sem = threading.Semaphore(1)

    output = {}
    for channel, model_path in zip(detection_channels, deepblink_models):
        model = pink.io.load_model(model_path)
        detections = []
        for img in preprocessed:
            detections.append(deepblink_spot_detection_task.submit(
                image=img,
            detection_channel=channel,
            out_dir=out_dir,
            model=model,
            gpu_sem=gpu_sem,
            ))

        output[channel] = detections

    logger.info(f"output from task - {output}")
    logger.info("finished sub flow")


@prefect.flow(
    name="DeepBlink Spot Detection",
    cache_result_in_memory=False,
    persist_result=True,
    result_serializer=cpr_serializer(),
    validate_parameters=False,
)
def run_deepblink(
    input_path: Path = "/path/to/acquisition/dir",
    output_path: Path = "/path/to/output/dir",
    pattern: str = "*.tif",
    detection_channels: List[int] = [0],
    deepblink_models: List[Path] = ["/path/to/model.h5"],
):
    images = [ImageSource.from_path(p) for p in glob(join(input_path,
                                                          pattern))]

    images_dicts = [img.serialize() for img in images]

    parameters = {
        "serialized_preprocessed": images_dicts,
        "out_dir": output_path,
        "detection_channels": detection_channels,
        "deepblink_models": deepblink_models,
    }

    run: FlowRun = run_deployment(
        name="deepblink/default",
        parameters=parameters,
        client=get_client(),
    )

    detections = run.state.result()
    return detections
