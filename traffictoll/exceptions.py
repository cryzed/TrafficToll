class TrafficTollException(Exception):
    pass


class MissingDependencyError(TrafficTollException):
    pass


class DependencyOutputError(TrafficTollException):
    pass


class ConfigError(TrafficTollException):
    pass
