from gr00t.experiment.data_config import So100DataConfig


class So101DualCamDataConfig(So100DataConfig):
    """SO-101 dual-camera (front + wrist) data config for GR00T N1.5."""

    video_keys = ["video.front", "video.wrist"]
