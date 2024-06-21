# local dependencies
from core.business.base import CVPluginExecutor as BasePlugin

__VER__ = '1.0.0'


_CONFIG = {
  **BasePlugin.CONFIG,
  'VALIDATION_RULES': {
    **BasePlugin.CONFIG['VALIDATION_RULES'],
  },

  # Developer config
  "AI_ENGINE": ["lowres_general_detector"],
  "ALLOW_COMPARISON_WITH_NO_ANCHOR": False,
  "MAX_INPUTS_QUEUE_SIZE": 1,  # slow plugin - must process only current state/input
  'WARNING_ANCHOR_SAVE_FAIL_SEND_INTERVAL': 60,  # seconds
  #############################

  # Alerter config
  "ALERT_DATA_COUNT": 5,
  "ALERT_RAISE_CONFIRMATION_TIME": 20,
  "ALERT_LOWER_CONFIRMATION_TIME": 15,
  "ALERT_RAISE_VALUE": 60,
  "ALERT_LOWER_VALUE": 30,
  "ALERT_MODE": "mean",
  "ALERT_REDUCE_VALUE": False,

  'RE_RAISE_TIME': 3 * 60,  # seconds
  'FORCED_LOWER': True,
  'FORCED_LOWER_AFTER_RE_RAISE_TIME': 12 * 60,  # seconds
  #############################

  # User config
  "ANCHOR_MAX_SUM_PERSON_AREA": 0,  # percent
  "ANALYSIS_IGNORE_MAX_PERSON_AREA": 70,  # percent
  "ANALYSIS_IGNORE_MIN_PERSON_AREA": 0.5,  # percent

  "DEMO_MODE": False,
  "ANCHOR_RELOAD_PERIOD": 10 * 60,  # second
  #############################

}


class CvImageAnchorComparisonPlugin(BasePlugin):
  CONFIG = _CONFIG

  def __init__(self, **kwargs):
    super(CvImageAnchorComparisonPlugin, self).__init__(**kwargs)
    return

  def startup(self):
    super(CvImageAnchorComparisonPlugin, self).startup()
    self.__last_capture_image = None
    self._anchor_last_save_time = 0
    self._force_save_anchor = False
    self._anchor = None

    self._last_anchor_validation_failed_time = 0

    self._alerter_re_raised = False
    self._alerter_forced_lower = False

    if self.cfg_forced_lower:
      assert self.cfg_re_raise_time is not None, "Forced lower cannot be configured with no re raise time!"
      assert self.cfg_forced_lower_after_re_raise_time is not None, "Invalid forced lower after re raise time '{}'".format(
        self.cfg_forced_lower_after_re_raise_time
      )

    self._maybe_load_anchor()
    return

  def _can_save_newer_anchor(self):
    """
    Check if we can save a new anchor. 
    We check if the anchor is expired, if there are alerts raised, if the anchor is not saved yet, etc.

    Returns
    -------
    bool
        True if we can save a newer anchor, False otherwise
    """
    if self._force_save_anchor:
      return True

    is_alert = self.alerter_is_alert()
    last_alert_value = self.alerter_get_last_value()

    # the alerter should be lowered and the last value in the alerter should not be higher than the raise value
    alerter_ok = not is_alert and (last_alert_value is None or last_alert_value < self.cfg_alert_raise_value)

    if self.serving_anchor_reload_period is None:
      # we will never reload the anchor
      return False

    anchor_expired = self.time() - self._anchor_last_save_time > self.serving_anchor_reload_period

    return anchor_expired and alerter_ok

  def _maybe_save_anchor(self, image, object_detection_inferences):
    if not self._can_save_newer_anchor():
      return

    valid, reason = self._validate_image_as_anchor(image, object_detection_inferences)
    if valid:
      self.P("Saving new anchor. Reason: {}".format(reason))
      self._anchor = image
      self._anchor_last_save_time = self.time()
      self._force_save_anchor = False
      self.custom_save_anchor(image)

      self._last_anchor_validation_failed_time = 0
    else:
      current_time = self.time()
      if current_time - self._last_anchor_validation_failed_time >= self.cfg_warning_anchor_save_fail_send_interval:
        msg = "Anchor validation failed: {}".format(reason)
        self._create_abnormal_notification(
          msg=msg,
          displayed=True,
        )

        self._last_anchor_validation_failed_time = current_time
      return
    pass

  def _maybe_load_anchor(self):
    if self._anchor is not None:
      return

    result = self.custom_load_anchor()
    if result is None:
      return

    anchor, anchor_last_save_time = result

    self._anchor = anchor
    self._anchor_last_save_time = anchor_last_save_time
    self._last_anchor_validation_failed_time = 0

    return

  def _get_intersection_tlbr(self, t1, l1, b1, r1, t2, l2, b2, r2):
    """
    Get the intersection between two TLBRs

    Parameters
    ----------
    t1 : int
        Top coordinate of the first TLBR
    l1 : int
        Left coordinate of the first TLBR
    b1 : int
        Bottom coordinate of the first TLBR
    r1 : int
        Right coordinate of the first TLBR
    t2 : int
        Top coordinate of the second TLBR
    l2 : int
        Left coordinate of the second TLBR
    b2 : int
        Bottom coordinate of the second TLBR
    r2 : int
        Right coordinate of the second TLBR

    Returns
    -------
    (t, l, b, r) : Tuple[int, int, int, int]
        The intersection TLBR
    """
    t = max(t1, t2)
    l = max(l1, l2)
    b = min(b1, b2)
    r = min(r1, r2)

    if t >= b or l >= r:
      return None

    return t, l, b, r

  def _intersect_tlbrs_with_target_zone(self, object_detector_inferences):
    """
    Intersect the inferences with the target zone. Edit the inferences tlbr to be bounded by the target zone.

    Parameters
    ----------
    object_detector_inferences : List[dict]
        The inferences to be intersected with the target zone

    Returns
    -------
    List[dict]
        The intersected inferences
    """

    # if no target zone defined, then we do not intersect
    if self._coords_type == self.ct.COORDS_NONE:
      return object_detector_inferences

    _t, _l, _b, _r = self._top, self._left, self._bottom, self._right

    lst_intersect_inferences = []
    for dct_inference in object_detector_inferences:
      t, l, b, r = dct_inference['TLBR_POS']
      intersection_tlbr = self._get_intersection_tlbr(_t, _l, _b, _r, t, l, b, r)
      if intersection_tlbr is None:
        continue

      lst_intersect_inferences.append({
        **dct_inference,
        'TLBR_POS': intersection_tlbr,
      })

    return lst_intersect_inferences

  def _validate_image_as_anchor(self, image, object_detection_inferences):
    people_areas = self.__people_areas_prc(image, object_detection_inferences)

    total_area = sum(people_areas) * 100

    return total_area < self.cfg_anchor_max_sum_person_area, "Total people area occupied in scene {}".format(total_area)

  def _maybe_force_lower(self):
    """
    Check if the alert needs to be force lowered. This method assumes the alerter is already re-raised.

    Parameters
    ----------
    is_alert : bool
        _description_

    Returns
    -------
    _type_
        _description_
    """
    if self.cfg_forced_lower is None or not self.cfg_forced_lower:
      return False

    if self.alerter_time_from_last_change() is None:
      # this should never happen
      return False

    # this time does not take into account the re-raise time
    time_since_last_raise = self.alerter_time_from_last_change()

    force_lower_threshold = self.cfg_forced_lower_after_re_raise_time + self.cfg_re_raise_time

    force_lower_time_elapsed = time_since_last_raise > force_lower_threshold

    return force_lower_time_elapsed

  def _maybe_re_raise_alert(self):
    """
    Check if the alert needs to be re-raised. This method assumes the alerter is already raised.

    Returns
    -------
    bool
        True if the alert needs to be re-raised, False otherwise
    """
    if self.cfg_re_raise_time is None:
      return False

    if self.alerter_time_from_last_change() is None:
      return False

    time_since_last_raise = self.alerter_time_from_last_change()
    re_raise_time_elapsed = time_since_last_raise > self.cfg_re_raise_time

    return re_raise_time_elapsed and not self._alerter_re_raised

  def _custom_alerter_status_changed(self):
    """
    Check if the alerter status changed. This is a custom implementation for the alerter status change
    that takes into account the re-raise and force lower logic

    Returns
    -------
    bool, current_alerter_status
        True if the alerter status changed, False otherwise
    """
    # get the current state of the alerter
    is_alert = self.alerter_is_alert()

    is_new_raised = self.alerter_is_new_raise()
    is_new_lower = self.alerter_is_new_lower()

    if is_new_raised:
      # if status changed from lower to raised
      self._alerter_re_raised = False
      return True, "Raised"

    if is_new_lower:
      self._alerter_forced_lower = False
      self._alerter_re_raised = False
      return True, "Lower"

    # the logic is the following:
    # 1. raise the alert if necessary, T=0
    # 2. if the alert is raised and T > T_reraise, then re-raise the alert
    # 3. if alert is raised and re-raised and T > T_reraise + T_forced_lower, then force lower the alert

    if not is_alert:
      # if the alerter is not raised, then we do not need to check for re-raise or force lower
      self._alerter_forced_lower = False
      self._alerter_re_raised = False
      return False, "OK"

    need_to_re_raise = self._maybe_re_raise_alert()

    if need_to_re_raise:
      self._alerter_re_raised = True
      return True, "Re-Raised"

    if self._alerter_re_raised:
      need_to_force_lower = self._maybe_force_lower()

      if need_to_force_lower:
        self._alerter_forced_lower = True
        return True, "Forced Lower"

    return False, "Alert"

  def _draw_witness_image(self, img, object_detector_inferences, debug_results, **kwargs):
    for dct_inference in object_detector_inferences:
      if dct_inference['TYPE'] == 'person':
        t, l, b, r = dct_inference['TLBR_POS']
        img = self._painter.draw_detection_box(
            image=img,
            top=t,
            left=l,
            bottom=b,
            right=r,
            label=str(dct_inference['TRACK_ID']),
            color=self.consts.GREEN
        )
    return img

  def _create_witness_image(self, original_image, debug_results, object_detector_inferences, **kwargs):
    return self.get_witness_image(
      img=original_image,
      draw_witness_image_kwargs={
        'object_detector_inferences': object_detector_inferences,
        'debug_results': debug_results,
      }
    )

  def _get_payload(self, alerter_status, **kwargs):
    # choose the cache based on the alerter status
    cache_witness = self.get_current_witness_kwargs(demo_mode=self.cfg_demo_mode)
    cache_witness_debug_info = cache_witness['debug_info']
    cache_witness_original_image = cache_witness['original_image']
    cache_witness_inferences = cache_witness['inferences']
    cache_witness_debug_results = cache_witness['debug_results']

    object_detector_inferences = cache_witness_inferences[self._get_detector_ai_engine()][0]

    for dct_debug_info in cache_witness_debug_info:
      self.add_debug_info(**dct_debug_info)
    self.add_debug_info({'Alerter Status': alerter_status})

    debug_results = cache_witness_debug_results
    original_image = cache_witness_original_image

    # create witness
    img_witness = self._create_witness_image(
      original_image=original_image,
      debug_results=debug_results,
      object_detector_inferences=object_detector_inferences,
    )

    # reset alerter if forced lower
    payload_kwargs = {
      'is_re_raise': self._alerter_re_raised if not self._alerter_forced_lower else False,
      'is_forced_lower': self._alerter_forced_lower,
      'is_new_re_raise': self._alerter_re_raised if not self._alerter_forced_lower else False,
      'is_new_forced_lower': self._alerter_forced_lower,
      'is_alert_status_changed': self.alerter_status_changed() or (self._alerter_re_raised and not self._alerter_forced_lower) or self._alerter_forced_lower,
    }

    if self._alerter_re_raised and not self._alerter_forced_lower:
      alert_first_raise_time = self.time() - self.alerter_time_from_last_change()
      alert_first_raise_datetime = self.datetime.fromtimestamp(alert_first_raise_time)
      str_alert_first_raise = self.datetime.strftime(alert_first_raise_datetime, "%Y-%m-%d %H:%M:%S.%f")

      payload_kwargs['status'] = f'Alert re-raised (first raise at {str_alert_first_raise})'
    elif self._alerter_forced_lower:
      alert_first_raise_time = self.time() - self.alerter_time_from_last_change()
      alert_first_raise_datetime = self.datetime.fromtimestamp(alert_first_raise_time)
      str_alert_first_raise = self.datetime.strftime(alert_first_raise_datetime, "%Y-%m-%d %H:%M:%S.%f")

      alert_re_raise_time = self.time() - self.alerter_time_from_last_change() + self.cfg_re_raise_time
      alert_re_raise_datetime = self.datetime.fromtimestamp(alert_re_raise_time)
      str_alert_re_raise = self.datetime.strftime(alert_re_raise_datetime, "%Y-%m-%d %H:%M:%S.%f")

      payload_kwargs['status'] = f'Alert forced-lower (first raise at {str_alert_first_raise} and re-raised at {str_alert_re_raise})'

    if alerter_status == "Forced Lower":
      self._force_save_anchor = True
      self._maybe_save_anchor(original_image, object_detector_inferences)
      self.alerter_hard_reset()
      self._alerter_re_raised = False
      self._alerter_forced_lower = False

    # create payload
    payload = self._create_payload(
      # we no longer send the original image because we have IMG_ORIG set to True
      # img=[img_witness, original_image, self._anchor],
      img=[img_witness, self._anchor],
      **payload_kwargs,
    )

    return payload

  def __people_areas_prc(self, image, lst_obj_inferences):
    if self._coords_type == self.ct.COORDS_NONE:
      H, W = image.shape[:2]
      image_area = H * W
    else:
      image_area = (self._bottom - self._top) * (self._right - self._left)

    person_areas = []
    for dct_inference in lst_obj_inferences:
      if dct_inference['TYPE'] == 'person':
        t, l, b, r = dct_inference['TLBR_POS']
        area = (r - l) * (b - t)
        person_areas.append(area)

    return [area / image_area for area in person_areas]

  def _validate_image_for_analysis(self, image, lst_obj_inferences):
    return self._validate_detections_for_analysis(image, lst_obj_inferences) and self.custom_image_validation(image)

  def _validate_detections_for_analysis(self, image, lst_obj_inferences):
    people_areas = self.__people_areas_prc(image, lst_obj_inferences)

    # check if all persons are smaller than a percentage
    all_persons_small = all([area * 100 <= self.cfg_analysis_ignore_min_person_area for area in people_areas])

    # check if there is a person bigger than a percentage
    exists_person_big = any([area * 100 >= self.cfg_analysis_ignore_max_person_area for area in people_areas])

    return all_persons_small or exists_person_big

  def _cache_witness_image(self, img, object_detector_inferences, result, debug_results, **kwargs):
    human_readable_last_anchor_time = self.datetime.fromtimestamp(self._anchor_last_save_time)
    human_readable_last_anchor_time = self.datetime.strftime(human_readable_last_anchor_time, "%Y-%m-%d %H:%M:%S")

    debug_info = [
      {'value': self.__people_areas_prc(img, object_detector_inferences)},
      {'value': f"Last Anchor Time: {human_readable_last_anchor_time}   Anchor Save Period (s): {self.serving_anchor_reload_period}"},
    ]
    if self.cfg_demo_mode:
      if self.cfg_forced_lower:
        debug_info.append({
          'value': {
              'last_raise': self.alerter_time_from_last_change(),
              're-raise_time': self.cfg_re_raise_time,
              'force_lower_time': self.cfg_forced_lower_after_re_raise_time + self.cfg_re_raise_time,
          }})
      elif self.cfg_re_raise_time is not None:
        debug_info.append({
          'value': {
              'last_raise': self.alerter_time_from_last_change(),
              're-raise_time': self.cfg_re_raise_time,
          }})
    debug_info.append({'value': debug_results})

    self.update_witness_kwargs(
      witness_args={
        'debug_info': debug_info,
        'inferences': self.dataapi_inferences(),
        'debug_results': debug_results,
        'original_image': self.__last_capture_image,
      },
      pos=self.alerter_get_current_frame_state(result),
    )

    return

  def _print_alert_status_changed(self, current_alerter_status):
    if current_alerter_status not in ["OK", "Alert"]:
      color = None
      if current_alerter_status in ["Raised", "Re-Raised", "Forced Lower"]:
        color = "r"
      self.P("Alerter status changed to: {}".format(current_alerter_status), color=color)
    return

  def _process(self):
    payload = None

    self.__last_capture_image = self.dataapi_image()

    object_detector_inferences = self.dataapi_inferences()[self._get_detector_ai_engine()][0]

    object_detector_inferences = self._intersect_tlbrs_with_target_zone(object_detector_inferences)

    # save the anchor if just started (anchor is None) or if we are forced to save it (force_save_anchor is True)
    # otherwise save a new anchor after comparison
    if self._anchor is None or self._force_save_anchor:
      self._maybe_save_anchor(self.__last_capture_image, object_detector_inferences)
    if self._anchor is None and not self.cfg_allow_comparison_with_no_anchor:
      # if we do not have an anchor, we wait for it
      return

    # process the current image
    result, debug_result = self.compare_image_with_anchor(self.__last_capture_image)

    if result is not None or self.cfg_demo_mode:
      self._cache_witness_image(
        img=self.__last_capture_image,
        object_detector_inferences=object_detector_inferences,
        result=result,
        debug_results=debug_result,
      )

    # decide if we keep it or not,
    # based on the presence of people in the image
    if result is not None and self._validate_image_for_analysis(self.__last_capture_image, object_detector_inferences):
      # we have people in the image, and they do not occupy the whole image,
      # so we do not process it further
      self.alerter_add_observation(result)

      # save the anchor if we successfully compared the current image with the previous anchor
      # we do this because we want images that do not generate alerts
      # if we do not save the anchor here, we can risk saving an image for which
      # changes cannot be computed (when `result is None`)
      self._maybe_save_anchor(self.__last_capture_image, object_detector_inferences)
    else:
      # we keep the alerter in the middle until we have valid images to make a good decision
      middle_value = (self.cfg_alert_raise_value + self.cfg_alert_lower_value) / 2
      self.alerter_add_observation(middle_value)
    alerter_status_changed, current_alerter_status = self._custom_alerter_status_changed()

    if alerter_status_changed or self.cfg_demo_mode:

      self._print_alert_status_changed(current_alerter_status)

      payload = self._get_payload(
        alerter_status=current_alerter_status,
      )

    return payload

  def _on_command(self, data, get_last_witness=None, reset_anchor=None, **kwargs):
    if (isinstance(data, str) and data.upper() == 'GET_LAST_WITNESS') or get_last_witness:
      self.add_payload_by_fields(
        img=[self.__last_capture_image, self._anchor],
        on_command_request=data,
      )
    if (isinstance(data, str) and data.upper() == 'RESET_ANCHOR') or reset_anchor:
      self._anchor = None
      self._anchor_last_save_time = 0
      self._force_save_anchor = True
    return

  # Can be overridden by the user
  @property
  def serving_anchor_reload_period(self):
    """
    The serving anchor reload period. This is the period after which the anchor will be reloaded.
    This property can be used to add custom logic for the anchor reload period. 
    (Like in the IQA case where this period is taken from serving config)

    Returns
    -------
    int
        The serving anchor reload period
    """
    return self.cfg_anchor_reload_period

  def custom_load_anchor(self):
    """
    Custom anchor loading. This method can be used to add custom anchor loading logic.

    Returns
    -------
    (anchor, anchor_save_time) : (np.ndarray, int) | None
        anchor: The anchor image
        anchor_save_time: The last time the anchor was saved
    """
    # method must return an image that will be used as anchor and the last anchor reload time
    return None

  def custom_save_anchor(self, image):
    """
    Custom anchor saving. This method can be used to add custom anchor saving logic.

    Parameters
    ----------
    image : np.ndarray
        The image to be saved as anchor
    """
    return

  def custom_image_validation(self, image):
    """
    Custom image validation. This method can be used to add custom validation rules.
    This method is called after the default image validation.

    Parameters
    ----------
    image : np.ndarray
        The image to be validated

    Returns
    -------
    bool
        True if the image is valid, False otherwise
    """
    return True

  def compare_image_with_anchor(self, image):
    """
    Compare the current image with the anchor image

    Parameters
    ----------
    image : np.ndarray
        The image to be compared with the anchor

    Returns
    -------
    (result, debug_result) : Tuple[float, dict] | None
        result: The result of the comparison
        debug_result: The debug information to be added to the witness
    """
    return None
