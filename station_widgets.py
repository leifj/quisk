# Widgets that drive the station-wide status elements.
# Amplifier status is rendered by this module so it can be rearranged without editing quisk.py.

from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import threading
import wx
from quisk_widgets import QuiskText
from hermes.quisk_widgets import BottomWidgets as HermesWidgets

class BottomWidgets:
  """Combine the Hermes bottom widgets with a station amp-status indicator."""
  def __init__(self, app, hardware, conf, frame, gbs, vertBox):
    self.application = None
    self.hardware = hardware
    self.conf = conf
    self.frame = frame
    self.gbs = gbs
    self.hermes_widgets = HermesWidgets(app, hardware, conf, frame, gbs, vertBox)
    self.application = self.hermes_widgets.application
    self.start_row = getattr(self.hermes_widgets, 'start_row', app.widget_row)
    self.start_col = getattr(self.hermes_widgets, 'start_col', app.button_start_col)
    self.switch_ports = ['0', '1', '2', '3', '4', '5']
    self.loop_modes = ['LoopA', 'LoopB', 'Crossed', 'Vertical']
    self._updating_switch_port = False
    self._updating_loop_mode = False
    self.xpa_relay_buttons = []
    self.num_rows_added = getattr(self.hermes_widgets, 'num_rows_added', 0)
    self._CreateStatusWidgets()
    # num_rows_added is updated inside _CreateStatusWidgets based on active equipment

  def _CreateStatusWidgets(self):
    eq = frozenset(getattr(self.hardware, 'station_equipment',
                           {'mqtt', 'xpa125b', 'atr1000', 'lz1aq', 'switchctl', 'atu100'}))
    has_xpa = 'xpa125b' in eq
    has_atr = 'atr1000' in eq
    has_lz1aq = 'lz1aq' in eq
    has_switchctl = 'switchctl' in eq
    has_atu = 'atu100' in eq
    height = self._StatusHeight()
    # Status text widgets are always created; hardware references them via self.application.X_status
    self.application.swr_status = QuiskText(self.frame, ' SWR 1.000  F 0.000  R 0.000 ', height, 0, False)
    self.application.swr_status.SetLabel(' SWR: --- ')
    self.application.amp_status = QuiskText(self.frame, ' AMP: Unknown ', height, 0, False)
    self.application.amp_status.SetLabel(' AMP: Unknown ')
    self.application.atr1000_status = QuiskText(self.frame, ' ATR: SWR 9.99  999W  LC  99.9uH  999pF  Bypass ', height, 0, False)
    self.application.atr1000_status.SetLabel(' ATR: --- ')
    self.application.atu100_status = QuiskText(self.frame, ' ATU: SWR 9.99  999W  99.9uH  999pF  Auto ', height, 0, False)
    self.application.atu100_status.SetLabel(' ATU: --- ')
    if has_switchctl:
      self.btnSwitchPower = self.application.QuiskCheckbutton(self.frame, self.OnBtnSwitchPower, text='SW Pwr')
      self.btnSwitchPower.SetValue(False, False)
      self.choiceSwitchPort = wx.Choice(self.frame, choices=self.switch_ports)
      self.choiceSwitchPort.SetSelection(0)
      self.choiceSwitchPort.SetMinSize((60, -1))
      self.frame.Bind(wx.EVT_CHOICE, self.OnChoiceSwitchPort, self.choiceSwitchPort)
      self.choiceSwitchPort.SetBackgroundColour(self.conf.color_entry)
      self.choiceSwitchPort.SetForegroundColour(self.conf.color_entry_txt)
    if has_lz1aq:
      self.btnLoopPower = self.application.QuiskCheckbutton(self.frame, self.OnBtnLoopPower, text='Loop Pwr')
      self.btnLoopPower.SetValue(False, False)
      self.choiceLoopMode = wx.Choice(self.frame, choices=self.loop_modes)
      self.choiceLoopMode.SetSelection(0)
      self.choiceLoopMode.SetMinSize((90, -1))
      self.frame.Bind(wx.EVT_CHOICE, self.OnChoiceLoopMode, self.choiceLoopMode)
      self.choiceLoopMode.SetBackgroundColour(self.conf.color_entry)
      self.choiceLoopMode.SetForegroundColour(self.conf.color_entry_txt)
    self.btnIoBoard = self.application.QuiskCheckbutton(self.frame, self.OnBtnIoBoardToggle, text='Alt RX')
    self.btnIoBoard.SetValue(False, False)
    self.btnIoBoard.Enable(False)
    if has_xpa:
      relay_config = None
      if hasattr(self.hardware, 'GetXpa125bRelayButtonConfig'):
        relay_config = self.hardware.GetXpa125bRelayButtonConfig()
      if not relay_config:
        relay_config = (
          {"label": "Power", "power_button": True},
          {"label": "Relay 1"},
          {"label": "Relay 2"},
          {"label": "Relay 3"},
        )
      for relay_index, config in enumerate(relay_config):
        label = config.get("label", "Relay %d" % (relay_index + 1))
        button = self.application.QuiskPushbutton(self.frame, self.OnBtnXpaRelay, label, use_right=True)
        button.xpa_base_label = label
        button.xpa_relay_index = relay_index
        button.xpa_power_button = bool(config.get("power_button", False))
        self.xpa_relay_buttons.append(button)
    for ctrl_name in ('swr_status', 'amp_status', 'atr1000_status', 'atu100_status'):
      tc = getattr(self.application, ctrl_name, None)
      if tc:
        tc.TextCtrl.SetBackgroundColour(self.conf.color_freq)
        tc.TextCtrl.SetForegroundColour(self.conf.color_freq_txt)
    # --- Layout ---
    amp_row = self.start_row + self.num_rows_added
    rows_added = 1
    if self.conf.button_layout == 'Large screen':
      col = self.start_col
      if has_switchctl:
        self.gbs.Add(self.btnSwitchPower, (amp_row, col), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.choiceSwitchPort, (amp_row, col + 2), (1, 2), flag=wx.EXPAND)
        col += 4
      if has_lz1aq:
        self.gbs.Add(self.btnLoopPower, (amp_row, col), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.choiceLoopMode, (amp_row, col + 2), (1, 3), flag=wx.EXPAND)
        col += 5
      self.gbs.Add(self.btnIoBoard, (amp_row, col), (1, 2), flag=wx.EXPAND)
      col += 2
      self.gbs.Add(self.application.swr_status, (amp_row, col), (1, 3), flag=wx.EXPAND)
      col += 3
      if has_xpa:
        self.gbs.Add(self.application.amp_status, (amp_row, col), (1, 3), flag=wx.EXPAND)
      relay_row = amp_row
      if has_xpa and self.xpa_relay_buttons:
        relay_row = amp_row + 1
        rows_added += 1
        for i, button in enumerate(self.xpa_relay_buttons):
          self.gbs.Add(button, (relay_row, self.start_col + i * 2), (1, 2), flag=wx.EXPAND)
      if has_atr:
        tuner_row = relay_row + 1
        rows_added += 1
        self.btnAtr1000FineTune = self.application.QuiskPushbutton(self.frame, self.OnBtnAtr1000FineTune, 'Fine Tune')
        self.btnAtr1000MemSelect = wx.Choice(self.frame, choices=['-- Mem --'] + ['M%d' % i for i in range(1, 10)])
        self.btnAtr1000MemSelect.SetSelection(0)
        self.frame.Bind(wx.EVT_CHOICE, self.OnChoiceAtr1000Mem, self.btnAtr1000MemSelect)
        self.btnAtr1000MemSelect.SetBackgroundColour(self.conf.color_entry)
        self.btnAtr1000MemSelect.SetForegroundColour(self.conf.color_entry_txt)
        self.gbs.Add(self.btnAtr1000FineTune, (tuner_row, self.start_col + 0), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.btnAtr1000MemSelect, (tuner_row, self.start_col + 2), (1, 3), flag=wx.EXPAND)
        self.gbs.Add(self.application.atr1000_status, (tuner_row, self.start_col + 5), (1, 6), flag=wx.EXPAND)
      if has_atu:
        atu_row = (tuner_row if has_atr else relay_row) + 1
        rows_added += 1
        self.btnAtu100Tune = self.application.QuiskPushbutton(self.frame, self.OnBtnAtu100Tune, 'Tune')
        self.gbs.Add(self.btnAtu100Tune, (atu_row, self.start_col + 0), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.application.atu100_status, (atu_row, self.start_col + 2), (1, 6), flag=wx.EXPAND)
    else:
      col = self.start_col
      if has_switchctl:
        self.gbs.Add(self.btnSwitchPower, (amp_row, col), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.choiceSwitchPort, (amp_row, col + 2), flag=wx.EXPAND)
        col += 4
      if has_lz1aq:
        self.gbs.Add(self.btnLoopPower, (amp_row, col), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.choiceLoopMode, (amp_row, col + 2), flag=wx.EXPAND)
        col += 3
      self.gbs.Add(self.application.swr_status, (amp_row, col), (1, 2), flag=wx.EXPAND)
      col += 2
      if has_xpa:
        self.gbs.Add(self.application.amp_status, (amp_row, col), (1, 2), flag=wx.EXPAND)
        col += 2
      self.gbs.Add(self.btnIoBoard, (amp_row, col), flag=wx.EXPAND)
      relay_row = amp_row
      if has_xpa and self.xpa_relay_buttons:
        relay_row = amp_row + 1
        rows_added += 1
        for i, button in enumerate(self.xpa_relay_buttons):
          self.gbs.Add(button, (relay_row, self.start_col + i), flag=wx.EXPAND)
      if has_atr:
        tuner_row = relay_row + 1
        rows_added += 1
        self.btnAtr1000FineTune = self.application.QuiskPushbutton(self.frame, self.OnBtnAtr1000FineTune, 'Fine Tune')
        self.btnAtr1000MemSelect = wx.Choice(self.frame, choices=['-- Mem --'] + ['M%d' % i for i in range(1, 10)])
        self.btnAtr1000MemSelect.SetSelection(0)
        self.frame.Bind(wx.EVT_CHOICE, self.OnChoiceAtr1000Mem, self.btnAtr1000MemSelect)
        self.btnAtr1000MemSelect.SetBackgroundColour(self.conf.color_entry)
        self.btnAtr1000MemSelect.SetForegroundColour(self.conf.color_entry_txt)
        self.gbs.Add(self.btnAtr1000FineTune, (tuner_row, self.start_col + 0), flag=wx.EXPAND)
        self.gbs.Add(self.btnAtr1000MemSelect, (tuner_row, self.start_col + 1), (1, 2), flag=wx.EXPAND)
        self.gbs.Add(self.application.atr1000_status, (tuner_row, self.start_col + 3), (1, 4), flag=wx.EXPAND)
      if has_atu:
        atu_row = (tuner_row if has_atr else relay_row) + 1
        rows_added += 1
        self.btnAtu100Tune = self.application.QuiskPushbutton(self.frame, self.OnBtnAtu100Tune, 'Tune')
        self.gbs.Add(self.btnAtu100Tune, (atu_row, self.start_col + 0), flag=wx.EXPAND)
        self.gbs.Add(self.application.atu100_status, (atu_row, self.start_col + 1), (1, 5), flag=wx.EXPAND)
    self.num_rows_added += rows_added
    self._RefreshSwitchCtlControls()
    self._RefreshLz1aqControls()
    self._RefreshIoBoardToggle()
    self._RefreshXpaRelayControls()

  def OnBtnSwitchPower(self, event):
    enabled = bool(self.btnSwitchPower.GetValue())
    if hasattr(self.hardware, 'SetSwitchCtlPower'):
      self.hardware.SetSwitchCtlPower(enabled)

  def OnChoiceSwitchPort(self, event):
    if self._updating_switch_port:
      return
    selection = self.choiceSwitchPort.GetStringSelection()
    if hasattr(self.hardware, 'SetSwitchCtlPin'):
      self.hardware.SetSwitchCtlPin(selection)

  def _RefreshSwitchCtlControls(self):
    if not hasattr(self, 'btnSwitchPower'):
      return
    power = None
    if hasattr(self.hardware, 'GetSwitchCtlPower'):
      power = self.hardware.GetSwitchCtlPower()
    if power is not None and self.btnSwitchPower.GetValue() != bool(power):
      self.btnSwitchPower.SetValue(bool(power), False)
    pin = None
    if hasattr(self.hardware, 'GetSwitchCtlPin'):
      pin = self.hardware.GetSwitchCtlPin()
    if pin is not None and 0 <= int(pin) < len(self.switch_ports):
      new_index = int(pin)
      if self.choiceSwitchPort.GetSelection() != new_index:
        self._updating_switch_port = True
        self.choiceSwitchPort.SetSelection(new_index)
        self._updating_switch_port = False

  def OnBtnLoopPower(self, event):
    enabled = bool(self.btnLoopPower.GetValue())
    if hasattr(self.hardware, 'SetLz1aqPower'):
      self.hardware.SetLz1aqPower(enabled)

  def OnChoiceLoopMode(self, event):
    if self._updating_loop_mode:
      return
    mode = self.choiceLoopMode.GetStringSelection()
    if hasattr(self.hardware, 'SetLz1aqMode'):
      self.hardware.SetLz1aqMode(mode)

  def _RefreshLz1aqControls(self):
    if not hasattr(self, 'btnLoopPower'):
      return
    power = None
    if hasattr(self.hardware, 'GetLz1aqPower'):
      power = self.hardware.GetLz1aqPower()
    if power is not None and self.btnLoopPower.GetValue() != bool(power):
      self.btnLoopPower.SetValue(bool(power), False)
    mode = None
    if hasattr(self.hardware, 'GetLz1aqMode'):
      mode = self.hardware.GetLz1aqMode()
    if mode in self.loop_modes:
      new_index = self.loop_modes.index(mode)
      if self.choiceLoopMode.GetSelection() != new_index:
        self._updating_loop_mode = True
        self.choiceLoopMode.SetSelection(new_index)
        self._updating_loop_mode = False

  def OnBtnIoBoardToggle(self, event):
    value = 1 if self.btnIoBoard.GetValue() else 0
    if hasattr(self.hardware, 'SetIoBoardToggle'):
      self.hardware.SetIoBoardToggle(value)

  def OnBtnAtr1000FineTune(self, event):
    if hasattr(self.hardware, 'Atr1000FineTune'):
      threading.Thread(target=self.hardware.Atr1000FineTune, daemon=True).start()

  def OnBtnAtu100Tune(self, event):
    if hasattr(self.hardware, 'Atu100Tune'):
      threading.Thread(target=self.hardware.Atu100Tune, daemon=True).start()

  def OnChoiceAtr1000Mem(self, event):
    index = self.btnAtr1000MemSelect.GetSelection()
    if index < 1:
      return
    memory_id = index  # M1=index 1, M2=index 2, ...
    self.btnAtr1000MemSelect.SetSelection(0)
    if hasattr(self.hardware, 'Atr1000ApplyMemory'):
      threading.Thread(target=self.hardware.Atr1000ApplyMemory, args=(memory_id,), daemon=True).start()

  def OnBtnXpaRelay(self, event):
    button = event.GetEventObject()
    relay_index = getattr(button, 'xpa_relay_index', None)
    if relay_index is None:
      return
    is_long = getattr(button, 'direction', 1) < 0
    if hasattr(self.hardware, 'PressXpa125bRelay'):
      self.hardware.PressXpa125bRelay(relay_index, is_long=is_long)

  def _RefreshIoBoardToggle(self):
    available = False
    if hasattr(self.hardware, 'IsIoBoardAvailable'):
      available = self.hardware.IsIoBoardAvailable()
    self.btnIoBoard.Enable(available)
    value = False
    if hasattr(self.hardware, 'GetIoBoardToggle'):
      value = bool(self.hardware.GetIoBoardToggle())
    if self.btnIoBoard.GetValue() != value:
      self.btnIoBoard.SetValue(value, False)

  def _RefreshXpaRelayControls(self):
    if not self.xpa_relay_buttons:
      return
    relay_config = None
    if hasattr(self.hardware, 'GetXpa125bRelayButtonConfig'):
      relay_config = self.hardware.GetXpa125bRelayButtonConfig()
    elif hasattr(self.hardware, 'GetXpa125bRelayLabels'):
      labels = self.hardware.GetXpa125bRelayLabels()
      relay_config = tuple({"label": label} for label in labels)
    connected = True
    if hasattr(self.hardware, 'GetXpa125bConnected'):
      connected = self.hardware.GetXpa125bConnected()
    sync_in_progress = bool(getattr(self.hardware, 'xpa125b_sync_in_progress', False))
    for index, button in enumerate(self.xpa_relay_buttons):
      if relay_config and index < len(relay_config):
        button.xpa_base_label = relay_config[index].get("label", getattr(button, 'xpa_base_label', button.GetLabel()))
      label = getattr(button, 'xpa_base_label', button.GetLabel())
      if getattr(button, 'xpa_power_button', False):
        if sync_in_progress:
          label = "%s (SYNC...)" % label
        elif not connected:
          label = "%s (OFFLINE)" % label
      if button.GetLabel() != label:
        button.SetLabel(label)
      button.Enable(connected)

  def _StatusHeight(self):
    smeter = getattr(self.application, 'smeter', None)
    if smeter:
      _, height = smeter.TextCtrl.GetMinSize()
      return height
    return self.frame.GetCharHeight() * 2

  def UpdateText(self):
    self.hermes_widgets.UpdateText()
    self._RefreshSwitchCtlControls()
    self._RefreshLz1aqControls()
    self._RefreshIoBoardToggle()
    self._RefreshXpaRelayControls()

  def __getattr__(self, name):
    return getattr(self.hermes_widgets, name)
