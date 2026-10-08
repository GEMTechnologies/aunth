"""One-off evidence probe: what, if anything, exists in Redis right now.

Read-only. Reports server info, keyspace sizes, and a bounded sample of keys.
Does not write, delete or mutate anything.
"""
from __future__ import annotations

import json
import sys

import redis


def main() -> int:
    # `socket_connect_timeout` alone is not enough: it bounds the CONNECT. A Redis that accepts
    # the connection and then stops answering needs `socket_timeout`, which redis-py otherwise
    # leaves as None - block forever. This probe's whole job is to decide whether Redis is
    # healthy, so a probe that hangs on a wedged Redis answers nothing.
    r = redis.Redis(
        host="127.0.0.1",
        port=6379,
        socket_connect_timeout=3,
        socket_timeout=5,
        decode_responses=True,
    )
    try:
        r.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"REDIS UNREACHABLE: {exc}")
        return 1

    info = r.info()
    print(f"redis_version={info.get('redis_version')}")
    print(f"uptime_in_seconds={info.get('uptime_in_seconds')}")
    print(f"db_keys={info.get('db0', {}).get('keys', 'n/a')}")
    print(f"total_commands_processed={info.get('total_commands_processed')}")
    print(f"total_connections_received={info.get('total_connections_received')}")

    dbsize = r.dbsize()
    print(f"DBSIZE={dbsize}")

    print("KEYSPACE:")
    keyspace = r.info("keyspace")
    if isinstance(keyspace, str):
        for line in keyspace.splitlines():
            print(f"  {line}")
    else:
        print(f"  {json.dumps(keyspace, indent=2) if keyspace else '(empty - no logical databases in use)'}")

    # Streams / pub-sub tell us whether an event pipeline ever ran here.
    try:
        print(f"PUBSUB_CHANNELS={r.pubsub_channels()}")
        print(f"PUBSUB_NUMSUB={r.pubsub_numsub()}")
    except Exception as exc:  # noqa: BLE001
        print(f"pubsub check failed: {exc}")

    if dbsize:
        sample = r.scan(count=200, _limit=2000)
        keys = list(sample[1])[:200]
        print(f"SAMPLE_KEYS ({len(keys)}):")
        print(json.dumps(keys, indent=2))
        for key in keys[:20]:
            try:
                print(f"  {key} :: {r.type(key)} :: ttl={r.ttl(key)}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {key} :: <error {exc}>")
    else:
        print("NO KEYS PRESENT.")

    r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
