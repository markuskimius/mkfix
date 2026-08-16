"""mkfix — FIX protocol testing engine built on mkio and mkui."""

__version__ = "0.6.2"


def serve(config="mkfix.toml", host=None, port=None, db_path=None):
    from mkfix.__main__ import serve as _serve
    _serve(config, host=host, port=port, db_path=db_path)
