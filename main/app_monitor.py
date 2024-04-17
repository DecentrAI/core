import json
import platform
import numpy as np
import gc

from core import constants as ct

from collections import deque, defaultdict
from core import DecentrAIObject
from core.utils.sys_mon import SystemMonitor

MAX_LOG_SIZE = 10_000

BASE_INCREMENT_MB = 250
MIN_AVAIL_DISK_GB = 10


MIN_GLOBAL_INCREASE_GB = BASE_INCREMENT_MB * 2 / 1000
MIN_PROCESS_INCREASE_MB = BASE_INCREMENT_MB
MIN_PROCESS_INCREASE_GB = MIN_PROCESS_INCREASE_MB / 1000

WARMUP_READINGS = 5

class ApplicationMonitor(DecentrAIObject):
  def __init__(self, log, owner, **kwargs):
    self.owner = owner
    self.avail_memory_log = deque(maxlen=MAX_LOG_SIZE)
    self.process_memory_log = deque(maxlen=MAX_LOG_SIZE)
    self._process_memory_log_inited = False
    self._avail_memory_log_inited = False
    self.gpu_log = {}
    self.cpu_log = deque(maxlen=MAX_LOG_SIZE)
    self.start_avail_memory = None
    self._max_avail_increase = MIN_GLOBAL_INCREASE_GB
    self._max_process_increase = MIN_PROCESS_INCREASE_MB
    self.start_process_memory = None
    self.alert = False
    self.alert_avail = 0 # 0: no alert, 1: increase alert, 2: overall low mem alert    
    self.alert_process = False
    self.critical_alert = False
    self.owner_mem = -1
    self.__first_payload_prepared = False
    self.__first_ram_alert_raised = False
    self.__first_ram_alert_raised_time = 0
    self._done_first_smi_error = False
    self.dct_curr_nr = defaultdict(lambda:0)
    super(ApplicationMonitor, self).__init__(log=log, prefix_log='[AMON]', **kwargs)
    return
  
  @property
  def first_payload_prepared(self):
    return self.__first_payload_prepared
  
  def P(self, s, color=None, **kwargs):
    if color is None or (isinstance(color,str) and color[0] not in ['e', 'r']):
      color = ct.COLORS.STATUS
    super().P(s, prefix=False, color=color, **kwargs)
    return      
  
  def startup(self):
    super().startup()
    self.sys_mon = SystemMonitor(log=self.log, monitored_prefix=ct.THREADS_PREFIX, name='S_SysMon')
    if self.owner.cfg_system_monitor:
      self.sys_mon.start()
    return
  
  
  def get_opencv_info(self):
    result = ""
    try:
      import cv2
      result = cv2.getBuildInformation()
    except:
      pass
    return result
  
  def is_ram_alert(self):
    is_alert = self.avail_memory_log[-1] < (self.owner.cfg_min_avail_mem_thr * self.log.total_memory)
    if is_alert and not self.__first_ram_alert_raised:
      self.__first_ram_alert_raised = True
      self.__first_ram_alert_raised_time = self.log.time_to_str()
      self.P("ALERT: First memory alert raised. Dumping logs", color='r')
      stats = gc.get_stats()
      self.P("GC stats:\n{}".format(json.dumps(stats, indent=4)))      
      self.P("GC garbage:\n{}".format(json.dumps(gc.garbage, indent=4)))
      self.owner._save_object_tree(
        fn='obj_mem_alert.txt', 
        save_top_only=True, 
        top=150
      )
      gc.collect()
    return is_alert
  
  
  def is_critical_ram_alert(self):
    is_critical_alert = False
    if len(self.avail_memory_log) >= 2:
      is_critical_alert = self.avail_memory_log[-1] < (self.owner.cfg_critical_restart_low_mem * self.log.total_memory)
    return is_critical_alert
      
  
  @property
  def slow_process_memory_log(self):
    result = []
    l = len(self.process_memory_log)
    if l > 0:
      for i in list(reversed(list(range(1,100,10)))):
        if l > i:
          result.append(round(self.process_memory_log[-i], 4))    
    return result
  
  def log_status(self, color=None):
    if self.start_process_memory is None or self.start_avail_memory is None:      
      return
    delta_avail_start = self.start_avail_memory - self.avail_memory_log[-1]
    delta_avail_last = self.avail_memory_log[-2] - self.avail_memory_log[-1]
    delta_process_start = self.process_memory_log[-1] - self.start_process_memory
    delta_process_last =  self.process_memory_log[-1] -  self.process_memory_log[-2]
    avail_mem = self.avail_memory_log[-1]
    total_mem = self.log.total_memory
        
    np_a = np.array(self.process_memory_log)
    avg = np.mean(np_a[1:] - np_a[:-1])

    text = 'OK'
    if self.is_ram_alert():
      color = 'r'
      text = ' * * * LOW MEMORY: {:>6,.2f} < {:>6,.2f} ({} x {:>6,.2f}) Initial: {} * * * '.format(
        self.avail_memory_log[-1],  self.owner.cfg_min_avail_mem_thr * self.log.total_memory, self.owner.cfg_min_avail_mem_thr, self.log.total_memory,
        self.__first_ram_alert_raised_time,
      )
      # Check critical alert
      self.critical_alert = self.is_critical_ram_alert()
      # end critical alerting
    elif color is not None and color[0] == 'y':
      text = 'Potential memory leak'
 
      

    str_log  = "Memory status for '{}' v.{}/Lv.{} (aa/ap: {}/{}):".format(      
      self.owner.cfg_eeid, self.owner.__version__, self.log.version,
      self.alert_avail, self.alert_process,
    )
    str_log += "\n\r==========================================================================================="
    str_log += "\n\r==========================================================================================="
    str_log += "\n\r                       Status: {}".format(text + ' ({} readings)'.format(len(self.avail_memory_log)))
    str_log += "\n\r                       Global mem status/increase:"
    str_log += "\n\r                         Avail / Total             : {:>6,.2f} / {:>6,.2f} (GB)".format(avail_mem, total_mem)
    str_log += "\n\r                         Avail Start / Last decr.  : {:>6,.2f} / {:>6,.2f} (GB)".format(self.start_avail_memory, delta_avail_last)
    str_log += "\n\r                         Out-of-process mem loss   : {:>6,.2f} (GB)".format(delta_avail_start - delta_process_start)
    str_log += "\n\r                        -------------------------------------------------------------"
    str_log += "\n\r                       Process memory status and increase:"
    str_log += "\n\r                         Start / Now               : {:>6,.2f} / {:>6,.2f} (GB)".format(self.start_process_memory, self.process_memory_log[-1])
    str_log += "\n\r                         Inc / Last Inc / Mean Inc : {:>6,.2f} / {:>6,.2f} / {:6,.2f} (GB)".format(delta_process_start, delta_process_last, avg)
    if self.owner_mem > 0:
      str_log += "\n\r                         Object tree mem eval      :{:>3,.3f} GB".format(self.owner_mem / 1024**3)
    str_log += "\n\r==========================================================================================="
    str_log += "\n\r==========================================================================================="
    self.P(str_log, color=color)
    return
      
  
  def log_avail_memory(self):
    avail_memory = self.log.get_avail_memory(gb=True)
    self.avail_memory_log.append(avail_memory)
    if not self._avail_memory_log_inited:
      if len(self.avail_memory_log) >= WARMUP_READINGS:
        self.start_avail_memory = avail_memory
        self._avail_memory_log_inited = True
    else:
      if avail_memory < (self.owner.cfg_min_avail_mem_thr * self.log.total_memory):
        self.alert_avail = 2
      else:
        self.alert_avail = 0 
        if avail_memory < self.start_avail_memory:
          # check for abnormal increase
          delta_start = self.start_avail_memory - avail_memory        
          if delta_start >= (self._max_avail_increase + MIN_GLOBAL_INCREASE_GB):
            self.alert_avail = 1
            self._max_avail_increase = delta_start
          #endif
        #endif
      #endif
    #endif
    return avail_memory


  def log_process_memory(self):
    self.log.start_timer('log_proc_mem', section='app_mon')
    process_memory = self.log.get_current_process_memory(mb=False)
    self.process_memory_log.append(process_memory)
    if not self._process_memory_log_inited:
      if len(self.process_memory_log) >= WARMUP_READINGS :
        self.start_process_memory = process_memory
        self._process_memory_log_inited = True
    else:
      if process_memory > self.start_process_memory:
        # check for abnormal increase
        delta_start = process_memory - self.start_process_memory 
        if delta_start > (self._max_process_increase + MIN_GLOBAL_INCREASE_GB):
          self.alert_process = True
          self._max_process_increase = delta_start
        else:
          self.alert_process = False

    if False:
      # self.P("Calculating overall process memory...")
      self.log.start_timer('get_obj_size', section='app_mon')    
      self.owner_mem = self.log.get_obj_size(self.owner)
      self.log.end_timer('get_obj_size', section='app_mon')
      # self.P("Done calculating overall process memory.")
    self.log.end_timer('log_proc_mem', section='app_mon')
    return process_memory


  def add_data_info(self, val, stage):
    self.dct_curr_nr[stage] += val
    return
  
  def get_basic_perf_info(self):
    str_info = ''
    str_info += '\n\n'+ 40 * '=' + ' Basic load Info ' + 40 * '='
    cpu_loads = np.array(self.cpu_log).astype('float16')
    str_cpu_loads = ' '.join('{:>4.1f}'.format(x) for x in cpu_loads[-10:])
    str_info += "\n  Used CPU: '{}'".format(self.log.get_processor_platform())
    str_info += "\n    Avg. CPU load:      {:>4.1f}% ({} %load)".format(
      np.mean(self.cpu_log),
      str_cpu_loads,
    )
    str_info += "\n    Proc mem over time: {} (GB))".format(self.slow_process_memory_log)
    cuda = self.owner.serving_manager.default_cuda if self.owner.serving_manager is not None else 'cuda:0'
    if isinstance(cuda, str):
      cuda = cuda.lower().replace('cuda:','')
      try:
        cuda = int(cuda)
      except:
        cuda = 0
    if cuda in self.gpu_log:
      dct_gpu = self.gpu_log[cuda]
      gpu_name = dct_gpu.get(ct.GPU_INFO.NAME)
      gpu_used = dct_gpu.get(ct.GPU_INFO.GPU_USED)
      gpu_mem = dct_gpu.get(ct.GPU_INFO.ALLOCATED_MEM)
      gpu_total_mem = dct_gpu.get(ct.GPU_INFO.TOTAL_MEM)
      if (gpu_used is not None) and (None not in gpu_used):
        str_info += "\n  Used GPU: [{}] '{}'".format(cuda, gpu_name)
        cuda_loads = np.array(gpu_used).astype('float16')
        mem_loads = np.array(gpu_mem).astype('float16')
        str_cuda_loads = ' '.join('{:>4.1f}'.format(x) for x in cuda_loads[-10:])
        str_mem_loads = ' '.join('{:>4.1f}'.format(x) for x in mem_loads[-10:])
        str_info += "\n    Avg. GPU core used: {:>4.1f}% ({} %load)".format(
          np.mean(gpu_used),
          str_cuda_loads,
        )
        str_info += "\n    Avg. GPU mem load:  {:>4.1f}% ({} GB)".format(
          np.mean(gpu_mem) / gpu_total_mem * 100,
          str_mem_loads,
        )
      else:
        str_info += "\n  GPU issue: No GPU present or driver issue"
    return str_info
  
  def _add_gpu_data(self, lst_info):
    if not isinstance(lst_info, list):
      return
    for i,gpu in enumerate(lst_info):
      if i not in self.gpu_log:
        self.gpu_log[i] = {
          ct.GPU_INFO.TOTAL_MEM : gpu[ct.GPU_INFO.TOTAL_MEM],
          ct.GPU_INFO.NAME : gpu[ct.GPU_INFO.NAME],
          ct.GPU_INFO.GPU_USED : deque(maxlen=MAX_LOG_SIZE),
          ct.GPU_INFO.ALLOCATED_MEM : deque(maxlen=MAX_LOG_SIZE),
          }
      self.gpu_log[i][ct.GPU_INFO.GPU_USED].append(gpu[ct.GPU_INFO.GPU_USED])
      self.gpu_log[i][ct.GPU_INFO.ALLOCATED_MEM].append(gpu[ct.GPU_INFO.ALLOCATED_MEM])
    return
      

  def get_gpu_info(self):
    # self.log.start_timer('gpu_info', section='async_comm')
    info = self.log.gpu_info(mb=False) or "Pytorch or NVidia-SMI issue"
    # self.log.end_timer('gpu_info', skip_first_timing=False, section='async_comm')
    
    if isinstance(info, list):
      self._add_gpu_data(info)
    return info

  def get_status(self, status=None, full=True, send_log=False):
    machine_ip = self.log.get_localhost_ip()
    machine_memory = round(self.log.total_memory,3)
    cpu_usage = self.log.get_cpu_usage()
    self.cpu_log.append(cpu_usage)
    
    is_docker = self.owner.runs_in_docker
    docker_env = self.owner.docker_env
    docker_source = self.owner.docker_source
    
    # check & get mem statuses required for log_status
    avail_memory = round(self.log_avail_memory(),3)
    process_memory = round(self.log_process_memory(),3)
    # log status if required
    if (not self.alert_avail) and (not self.alert_process):
      self.alert = False
    else:
      self.log_status(color=None if self.alert_avail == 0 else 'r')    
    
    
    # hard check avail disk
    # TODO: automatic checker of info data post gathering
    total_disk = round(self.log.total_disk, 3)
    avail_disk = round(self.log.get_avail_disk(gb=True), 3)
    display = True
    if avail_disk < MIN_AVAIL_DISK_GB:
      info = "WARNING: Avail disk on '{}' current volume is {:.1f} GB below minimum of {:.1f} GB".format(
        self.owner.cfg_eeid,
        avail_disk,
        MIN_AVAIL_DISK_GB
      )
      if display:
        self.P(info, color='error')
      self._create_notification(
        notif=ct.STATUS_TYPE.STATUS_ABNORMAL_FUNCTIONING, 
        msg="LOW DISK SPACE ON '{}'".format(self.owner.cfg_eeid), 
        info=info,
        displayed=display,
      )
    
    if avail_memory < self.owner.cfg_min_avail_mem_thr * self.log.total_memory:
      info = "WARNING: available memory {:.2f} GB ({:.1f}%) below {:.2f} GB ({:.1f}%) threshold of total memory ({:.1f} GB)".format(
        avail_memory, avail_memory / self.log.total_memory * 100,
        self.owner.cfg_min_avail_mem_thr * self.log.total_memory, self.owner.cfg_min_avail_mem_thr * 100,
        self.log.total_memory,
      )
      if display:
        self.P(info, color='error')
      self._create_notification(
        notif=ct.STATUS_TYPE.STATUS_ABNORMAL_FUNCTIONING, 
        msg="LOW MEMORY ON '{}'".format(self.owner.cfg_eeid), 
        info=info,
        displayed=display,
      )
      
    # now get basic info
    
    str_cpu = self.log.get_processor_platform()
    
    total_time = self.owner.running_time
    timestamp = self.log.now_str(nice_print=False)
    current_time = self.log.now_str(nice_print=True, short=False) # leave with milis - modify downstream if necessary

    n_payloads = self.dct_curr_nr[ct.NR_PAYLOADS]
    n_inferences = self.dct_curr_nr[ct.NR_INFERENCES]
    n_stream_data = self.dct_curr_nr[ct.NR_STREAMS_DATA]
    self.dct_curr_nr = defaultdict(lambda:0)
    
    if status is None:
      status = ct.DEVICE_STATUS_ONLINE
    
    lst_gpus = self.get_gpu_info()
    
    loops_timings = {}
    if self.owner.comm_manager is not None:
      loops_timings = {
        'main_loop_avg_time' : self.owner.avg_loop_timings,
        'comm_loop_avg_time' : self.owner.comm_manager.avg_comm_loop_timings,
      }

    active_serving_processes = []
    serving_pids = []
    default_cuda = ''
    if self.owner.serving_manager is not None:
      active_serving_processes = self.owner.serving_manager.get_active_servers(show=False)
      default_cuda = self.owner.serving_manager.default_cuda
      serving_pids = self.owner.serving_manager.serving_pids
    str_active_serving_processes = '\n\nActive serving processes (in-process: {}):'.format(self.owner.in_process_serving)  
    if len(active_serving_processes) == 0:
      str_active_serving_processes += '\n  --  No running serving processes --'
    for svr in active_serving_processes:
      str_active_serving_processes += '\n'
      for k,v in svr.items():
        str_active_serving_processes += "\n    {:<15}{}".format(k + ':', v)

    str_gpu_info = self.get_basic_perf_info()
    
    str_timers = None
    if full:
      str_timers = '\n'.join(self.log.format_timers())
      # now get the sysmon info
      str_timers += '\n\n' + '\n'.join(self.get_sys_mon_info())
      # add package info
      str_timers +='\n\nPackage information (Python {}):\n{}'.format(
        self.log.python_version,
        self.log.get_packages(as_text=True, indent=4)
      )
      # add serving processes
      str_timers += str_active_serving_processes
      # add open-cv if available
      str_timers += "\n\n" + self.get_opencv_info()
    #endif prepare timers if necessary
    
    dct_cap_info = None
    if self.owner.capture_manager is not None:
      dct_cap_info = self.owner.capture_manager.get_captures_status(display=False, as_dict=True)
    
    dct_comm_info = None  
    inkB, outkB = 0, 0
    if self.owner.comm_manager is not None:
      dct_comm_info = self.owner.comm_manager.get_comms_status()
      for _, dct_c in dct_comm_info.items():
        inkB += dct_c[ct.HB.COMM_INFO.IN_KB]
        outkB += dct_c[ct.HB.COMM_INFO.OUT_KB]
      
    str_git_branch = self.sys_mon.git_branch
    str_conda_branch = self.log.conda_env
    
    if is_docker:
      str_git_branch = 'Docker:' + docker_source
      str_conda_branch = 'Env:' + docker_env      
    
    if send_log:
      error_log = self.log.err_log
      dev_log = self.log.app_log
    else:
      dev_log   = 'Device log available only upon request or in special situations'
      error_log = 'Error log available only upon request or in special situations'
    active_business_plugins = []
    if self.owner.business_manager is not None:
      active_business_plugins = self.owner.business_manager.get_active_plugins_instances(
        as_dict=True # send business plugin instances info as objects (NOT as lists)
      )
    

    s_os = platform.platform()
    s_os = s_os.replace('Windows', 'W').replace('Linux','L')
    _ver = self.owner.__version__ + ' L:{} '.format(self.log.version) + s_os
    lst_config_streams = []
    if self.owner.config_manager is not None:
      lst_config_streams = list(self.owner.config_manager.dct_config_streams.values())

    address = self.owner.e2_addr      
    is_supervisor = self.owner.is_supervisor_node
    
    whitelist = self.owner.whitelist

    dct_status = {
      #mandatory
      ct.HB.CURRENT_TIME      : current_time,
      
      ct.HB.EE_ADDR           : address,
      ct.HB.EE_WHITELIST      : whitelist,
      ct.HB.EE_IS_SUPER       : is_supervisor,
      ct.HB.EE_FORMATTER      : self.owner.cfg_io_formatter,
      
      # ALERTS
      ct.HB.IS_ALERT_RAM      : self.is_ram_alert(),
      # End ALERTS

      ct.HB.NR_INFERENCES     : n_inferences,
      ct.HB.NR_PAYLOADS       : n_payloads,
      ct.HB.NR_STREAMS_DATA   : n_stream_data,
      ct.HB.PY_VER            : self.log.python_version,

      ct.HB.SECURED           : self.owner.is_secured,

      ct.HB.EE_HB_TIME        : self.owner.cfg_app_seconds_heartbeat,
      ct.HB.DEVICE_STATUS     : status,
      ct.HB.MACHINE_IP        : machine_ip,
      ct.HB.MACHINE_MEMORY    : machine_memory,
      ct.HB.AVAILABLE_MEMORY  : avail_memory,
      ct.HB.PROCESS_MEMORY    : process_memory,      
      ct.HB.CPU_USED          : cpu_usage,
      ct.HB.GPUS              : lst_gpus,
      ct.HB.GPU_INFO          : str_gpu_info,
      ct.HB.DEFAULT_CUDA      : default_cuda,
      ct.HB.CPU               : str_cpu,
      ct.HB.TIMESTAMP         : timestamp,
      ct.HB.UPTIME            : total_time,
      ct.HB.VERSION           : _ver,
      ct.HB.LOGGER_VERSION    : self.log.version,
      ct.HB.TOTAL_DISK        : total_disk,
      ct.HB.AVAILABLE_DISK    : avail_disk,
      ct.HB.ACTIVE_PLUGINS    : active_business_plugins,
      ct.HB.STOP_LOG          : self.owner.main_loop_stop_log,
      
      ct.HB.GIT_BRANCH        : str_git_branch,
      ct.HB.CONDA_ENV         : str_conda_branch,      
        
      ct.HB.CONFIG_STREAMS    : lst_config_streams,
      ct.HB.DCT_STATS         : dct_cap_info,
      ct.HB.COMM_STATS        : dct_comm_info,
      ct.HB.COMM_INFO.IN_KB   : round(inkB, 3),
      ct.HB.COMM_INFO.OUT_KB  : round(outkB, 3),
        
      ct.HB.SERVING_PIDS      : serving_pids, 
      ct.HB.LOOPS_TIMINGS     : loops_timings,

      ct.HB.TIMERS            : 'Timers not available in summary',
      ct.HB.DEVICE_LOG        : dev_log,
      ct.HB.ERROR_LOG         : error_log
    }
    
    dct_ext_status = {    
      ct.HB.TIMERS            : str_timers,
    }
    
    
    if full:
      for k, v in dct_ext_status.items():
        dct_status[k] = v
        
    if self.owner.cfg_compress_heartbeat:
      str_text = "{}"
      try:
        str_text = json.dumps(dct_status)
      except Exception as exc:
        self.P("ERROR: Could not compress heartbeat: {}\n{}".format(exc, dct_status), color='error')
      str_encoded = self.log.compress_text(str_text)
      dct_status = {
        ct.HB.ENCODED_DATA : str_encoded,
        # needed in caller...
        ct.HB.NR_INFERENCES     : n_inferences,
        ct.HB.NR_PAYLOADS       : n_payloads,
        ct.HB.NR_STREAMS_DATA   : n_stream_data,        
        ct.HB.HEARTBEAT_VERSION : ct.HB.V2, 
      }
    else:
      dct_status[ct.HB.HEARTBEAT_VERSION] = ct.HB.V1
      
    if not self.first_payload_prepared:
      self.__first_payload_prepared = True
      self.P("First hb for {}".format(address), boxed=True)

    return dct_status
  
  
  def get_sys_mon_info(self):
    if hasattr(self, 'sys_mon') and self.sys_mon is not None:
      return self.sys_mon.get_status_log(descending=True)
    return []

  def log_sys_mon_info(self, color='n'):
    info = self.get_sys_mon_info()
    s = "\n" + "\n".join(info)
    self.log.P(s, color=color)
    return


  def _maybe_stop_sys_mon(self):
    if hasattr(self, 'sys_mon') and self.sys_mon is not None:
      self.sys_mon.stop()
      self.sys_mon.log_status()
    return
  

  def shutdown(self):
    super().shutdown()
    self._maybe_stop_sys_mon()
    