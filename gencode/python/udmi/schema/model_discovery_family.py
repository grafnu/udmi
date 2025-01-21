"""Generated class for model_discovery_family.json"""
from .events_discovery import DiscoveryEvents


class FamilyDiscoveryModel:
  """Generated schema class"""

  def __init__(self):
    self.generation = None
    self.scan_interval_sec = None
    self.scan_duration_sec = None
    self.discovered = None

  @staticmethod
  def from_dict(source):
    if not source:
      return None
    result = FamilyDiscoveryModel()
    result.generation = source.get('generation')
    result.scan_interval_sec = source.get('scan_interval_sec')
    result.scan_duration_sec = source.get('scan_duration_sec')
    result.discovered = DiscoveryEvents.from_dict(source.get('discovered'))
    return result

  @staticmethod
  def map_from(source):
    if not source:
      return None
    result = {}
    for key in source:
      result[key] = FamilyDiscoveryModel.from_dict(source[key])
    return result

  @staticmethod
  def expand_dict(input):
    result = {}
    for property in input:
      result[property] = input[property].to_dict() if input[property] else {}
    return result

  def to_dict(self):
    result = {}
    if self.generation:
      result['generation'] = self.generation # 5
    if self.scan_interval_sec:
      result['scan_interval_sec'] = self.scan_interval_sec # 5
    if self.scan_duration_sec:
      result['scan_duration_sec'] = self.scan_duration_sec # 5
    if self.discovered:
      result['discovered'] = self.discovered.to_dict() # 4
    return result
