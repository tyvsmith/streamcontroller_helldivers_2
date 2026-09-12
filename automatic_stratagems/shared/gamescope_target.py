"""Gamescope target identity shared by the host resolvers and their sandbox callers."""

from dataclasses import asdict, dataclass
import re


GAMESCOPECTL_PATH = ('/usr/lib/extensions/vulkan/gamescope/bin/'
                     'gamescopectl')
RECORD_NAME = 'steam-capture.json'


class HostMetadataError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlatpakGamescopeTarget:
    game_pid: int
    game_start_time: str
    sandbox_pid: int
    sandbox_start_time: str
    instance_id: str
    socket: str
    socket_dev: int
    socket_ino: int
    host_cache: str
    sandbox_cache: str
    cache_dev: int
    cache_ino: int
    gamescopectl: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__annotations__):
            raise HostMetadataError('Host Gamescope target response is invalid.')
        if (any(type(value.get(name)) is not int or value[name] <= 1
                for name in ('game_pid', 'sandbox_pid')) or
                any(type(value.get(name)) is not int or value[name] < 0
                    for name in ('socket_dev', 'socket_ino',
                                 'cache_dev', 'cache_ino')) or
                any(not isinstance(value.get(name), str) or
                    not value[name] or '\0' in value[name]
                    for name in ('game_start_time', 'sandbox_start_time',
                                 'instance_id', 'socket',
                                 'host_cache', 'sandbox_cache',
                                 'gamescopectl'))):
            raise HostMetadataError('Host Gamescope target response is invalid.')
        if (not value['game_start_time'].isdigit() or
                not value['sandbox_start_time'].isdigit() or
                not re.fullmatch(r'[A-Za-z0-9._-]{1,128}',
                                 value['instance_id']) or
                value['gamescopectl'] != GAMESCOPECTL_PATH):
            raise HostMetadataError('Host Gamescope target response is invalid.')
        return cls(**value)
