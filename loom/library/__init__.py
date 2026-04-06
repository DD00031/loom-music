# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.


import os
from sys import stderr

import confuse

from .util.deprecation import deprecate_imports

__version__ = "2.8.0"
__author__ = "Adrian Sampson <adrian@radbox.org>"


def __getattr__(name: str):
    """Handle deprecated imports."""
    return deprecate_imports(
        __name__,
        {"art": "loom.library.plugins._utils", "vfs": "loom.library.plugins._utils"},
        name,
    )


class IncludeLazyConfig(confuse.LazyConfig):
    """Confuse LazyConfig extended for loom:

    1. Reads the ``[library]`` section from the unified loom TOML config and
       overlays it on top of the built-in defaults.  This lets users configure
       the library manager (directory, plugins, import settings, paths…) from
       the same ``config.toml`` file that controls downloads.

    2. Merges any additional YAML files listed under an ``include`` key
       (original beets behaviour, kept for compatibility).
    """

    def read(self, user: bool = True, defaults: bool = True) -> None:
        # 1. Read built-in defaults (config_default.yaml) and any legacy
        #    ~/.config/loom/config.yaml the user may have.
        super().read(user, defaults)

        # 2. Overlay [library] section from the unified TOML config.
        #    self.set() inserts at the highest priority, so TOML wins over the
        #    legacy YAML and the built-in defaults.
        if user:
            try:
                import tomlkit
                from loom.download.config import DEFAULT_CONFIG_PATH

                if os.path.isfile(DEFAULT_CONFIG_PATH):
                    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
                        toml_data = tomlkit.load(fh)
                    library_section = toml_data.get("library", {})
                    if library_section:
                        self.set(dict(library_section))
            except Exception:
                # Never crash the library subsystem over a config read error.
                pass

        # 3. Process any `include:` paths listed in the config (original behaviour).
        try:
            for view in self["include"].sequence():
                self.set_file(view.as_filename())
        except confuse.NotFoundError:
            pass
        except confuse.ConfigReadError as err:
            stderr.write(f"configuration `include` failed: {err.reason}")


config = IncludeLazyConfig("loom", __name__)
