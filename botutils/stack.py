"""
Stack Counter
~~~~~~~~~~~~~~

Class for building up a counter over time for unique IDs

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from time import time


class Stack:
    """
    An object to stack up counters at an interval

    Parameters
    ----------
    interval : int
        the interval at which to add +1 to a IDs counter
    timeout : int, optional
        the seconds at which to reset on use if inactive for this period of time
    max_stack : int
        the counter limit
    """
    def __init__(self, interval, timeout, max_stack):
        self.interval = interval
        self.timeout = timeout
        self.max_stack = max_stack
        self.index = {}
        self._last_cleanup = time()

    def cleanup(self, now=None):
        now = time() if now is None else now
        if not self.timeout or now - self._last_cleanup < 60:
            return
        cutoff = now - self.timeout
        self.index = {
            unique_id: last_used
            for unique_id, last_used in self.index.items()
            if last_used >= cutoff
        }
        self._last_cleanup = now

    def get_stack(self, unique_id):
        """Gets the IDs number of stacked intervals"""
        self.cleanup()
        if unique_id in self.index:
            time_since = time() - self.index[unique_id]
            self.index[unique_id] = time()

            # Stacks expired due to not being used within the timeout
            if self.timeout and time_since > self.timeout:
                return 0

            # Limit the stack to `max_stack`
            stack = int(time_since / self.interval)
            if stack > self.max_stack:
                stack = self.max_stack

            return stack

        # For first use with a ID, set the last_used_time and return 0
        self.index[unique_id] = time()
        return 0
