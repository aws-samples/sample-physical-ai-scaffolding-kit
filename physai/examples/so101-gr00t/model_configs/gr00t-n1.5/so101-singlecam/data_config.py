from gr00t.experiment.data_config import So100DataConfig


class So101SingleCamDataConfig(So100DataConfig):
    """SO-101 single-camera (front only) data config for GR00T N1.5."""

    video_keys = ["video.front"]
