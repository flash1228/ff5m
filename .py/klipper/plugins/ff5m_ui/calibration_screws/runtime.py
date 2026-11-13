## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from copy import deepcopy

from ui import PageTree

from .actions import REPEAT, DONE, ScrewResultAction
from .page import PAGE, PAGE_BOUNDS, ScrewResultRef
from .state import ScrewResultState, result_values


def create_page(bounds=PAGE_BOUNDS):
    """Create an isolated tree of the declared page."""
    root = deepcopy(PAGE.root)
    result = PageTree(root, bounds, page_id=PAGE.page_key)
    result.title = PAGE.title
    result.show_back = PAGE.show_back
    return result
