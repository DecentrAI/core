from core.serving.default_inference.th_yf_base import YfBase as ParentServingProcess

_CONFIG = {
  **ParentServingProcess.CONFIG,

  "MODEL_WEIGHTS_FILENAME": "20230723_y8l_nms.ths",
  "MODEL_WEIGHTS_FILENAME_DEBUG": "20230723_y8l_nms_top6.ths",

  "URL": "minio:Y8/20230723_y8l_nms.ths",
  "URL_DEBUG": "minio:Y8/20230723_y8l_nms_top6.ths",

  'IMAGE_HW': (640, 896),

  'VALIDATION_RULES': {
    **ParentServingProcess.CONFIG['VALIDATION_RULES'],
  },
}

DEBUG_COVERED_SERVERS = False

__VER__ = '0.2.0.0'


class ThYf8l(ParentServingProcess):
  CONFIG = _CONFIG

  def __init__(self, **kwargs):
    super(ThYf8l, self).__init__(**kwargs)
    return
