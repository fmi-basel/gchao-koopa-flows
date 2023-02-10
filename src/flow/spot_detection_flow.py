from glob import glob
from os.path import join
from pathlib import Path
from typing import List

import prefect
from cpr.Serializer import cpr_serializer
from cpr.image.ImageSource import ImageSource
from prefect import get_client
from prefect.client.schemas import FlowRun
from prefect.deployments import run_deployment


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