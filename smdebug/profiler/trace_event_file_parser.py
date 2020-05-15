# First Party
# Standard Library
import datetime

from smdebug.core.logger import get_logger


class ThreadInfo:
    def __init__(self, tid, thread_name):
        self.tid = tid
        self.thread_name = thread_name


class ProcessInfo:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self._threads = dict()

    def add_thread(self, threadid, thread_name):
        self._threads[threadid] = ThreadInfo(threadid, thread_name)

    def get_thread_info(self, threadid):
        return self._threads[threadid]


class TraceEvent:
    def __init__(self, ts, name, dur, pid, tid, event_args):
        self.start_time = ts
        self.event_name = name
        self.duration = dur
        self.end_time = self.start_time + self.duration
        self.pid = pid
        self.tid = tid
        self.event_args = event_args


class TraceEventParser:
    def __init__(self):
        self._processes = dict()
        self._trace_events = list()
        """
        The _pid_stacks maintain the directory of stacks indexed using pid. The stack contains 'B' type events.
        The stack will be popped as we process the 'E' events for the same pid.
        """
        self._pid_stacks = dict()
        self._start_timestamp = 0
        self._start_time_known = False
        """
        In horovod trace, the 'ts' timestamps for events are relative to the first 'ts' timestamp included in the
        first event. We will consider this timestamp as base_timestamp and subtract it from the 'ts' values read
        from the subsequent events. It will give us 0-based timestamps for rest of the events. Please note that
        this base_timestamp is not related to unix epoch based timestamp. We would still have to add absolute start time
        (self._start_timestamp) to obtain the absolute start time of any event.
        """
        self._base_timestamp = 0
        self._base_timestamp_initialized = True
        # The timestamp in trace events are in micro seconds, we multiply by 1000 to convert to ns
        self._timescale_multiplier_for_ns = 1000
        self.logger = get_logger("smdebug-profiler")

    def read_trace_file(self):
        pass

    def _populate_process_info_for_metaevent(self, event):
        id = event["pid"]
        if event["name"] == "process_name":
            name = event["args"]["name"] if "name" in event["args"] else "Unknown"
            self._processes[id] = ProcessInfo(id, name)

    def _populate_thread_info_for_metaevent(self, event):
        if event["name"] == "thread_name":
            name = event["args"]["name"]
            t_id = event["tid"]
            pid = event["pid"]
            if pid not in self._processes:
                self.logger.warn(
                    f"Did not find matching process for pid {pid}. Creating a process with name 'Unknown'"
                )
                self._processes[pid] = ProcessInfo(pid, "Unknown")
            self._processes[pid].add_thread(t_id, name)

    def _populate_start_time(self, event):
        pass

    def _read_event(self, event):
        if "ph" not in event:
            self.logger.error(f"In correctly formatted trace file. The 'ph' field is not present")
            return
        phase_type = event["ph"]
        if "ts" in event and not self._base_timestamp_initialized:
            self._base_timestamp = event["ts"]
            self.logger.info(
                f"The base timestamp in horovod trace file for future events is {self._base_timestamp}"
            )
            self._base_timestamp_initialized = True
        if phase_type == "M":
            self._populate_process_info_for_metaevent(event)
            self._populate_thread_info_for_metaevent(event)
            self._populate_start_time(event)
        if phase_type == "X":
            # In nano seconds
            start_time = (
                event["ts"] - self._base_timestamp + self._start_timestamp
            ) * self._timescale_multiplier_for_ns
            # In nano seconds
            dur = event["dur"] * self._timescale_multiplier_for_ns
            name = event["name"]
            id = event["pid"]
            tid = event["tid"] if "tid" in event else "0"
            event_args = event["args"] if "args" in event else None
            t_event = TraceEvent(start_time, name, dur, id, tid, event_args)
            self._trace_events.append(t_event)
        if phase_type == "B":
            pid = event["pid"]
            if pid not in self._pid_stacks:
                self._pid_stacks[pid] = []
            self._pid_stacks[pid].append(event)
        if phase_type == "E":
            pid = event["pid"]
            if pid not in self._pid_stacks:
                self.logger.error(f"Did not find the 'B' type event in the pid {pid}")
            else:
                b_event = self._pid_stacks[pid][-1]
                self._pid_stacks[pid].pop()
                start_time = (
                    b_event["ts"] - self._base_timestamp + self._start_timestamp
                ) * self._timescale_multiplier_for_ns
                end_time = (
                    event["ts"] - self._base_timestamp + self._start_timestamp
                ) * self._timescale_multiplier_for_ns
                duration = end_time - start_time
                if duration < 0:
                    self.logger.error(
                        f"Error in reading the events 'B' and 'E' or trace file is corrupt: pid = "
                        f"{pid}, start_time = {b_event['ts']} end_time = {event['ts']} name = "
                        f"{b_event['name']}"
                    )
                    return
                tid = b_event["tid"] if "tid" in event else "0"
                name = b_event["name"]
                event_args = event["args"] if "args" in event else None
                t_event = TraceEvent(start_time, name, duration, pid, tid, event_args)
                self._trace_events.append(t_event)

    def get_all_events(self):
        return self._trace_events

    def get_events_start_time_sorted(self):
        return sorted(self._trace_events, key=lambda x: x.start_time)

    def get_events_end_time_sorted(self):
        return sorted(self._trace_events, key=lambda x: x.end_time)

    """
    Return the events that are in progress at the specified timestamp in seconds.
    For tracefiles generated by smdebug, the specified timestamp needs to be seconds elapsed from epoch ( January 1,
    1970 12:00:00 AM)
    For horovod and tensorboard generated tracefiles, the specified timestamp in seconds will be interpreted as
    seconds elapsed from the first recorded event.
    Performance of this function can be improved by implementing interval tree.
    """

    def get_events_at_timestamp_in_seconds(self, timestamp_in_seconds):
        timestamp_in_nanoseconds = timestamp_in_seconds * 1000000000
        result_events = list()
        for x_event in self._trace_events:
            if x_event.start_time <= timestamp_in_nanoseconds <= x_event.end_time:
                result_events.append(x_event)
        return result_events

    """
    The TraceEvent class can not support retrieving events at given datetime object.
    This is because only smdebug based tracefiles store the timestamps based on unix epoch timestamp.
    """

    def get_events_at_time(self, timestamp_datetime: datetime):
        return None

    """
    Return the events that have started and completed within the given start and end time boundaries.
    The start and end time are in seconds.
    For tracefiles generated by smdebug, the start and end timestamps need to be seconds elapsed
    from epoch ( January 1, 1970 12:00:00 AM)
    For horovod and tensorboard generated tracefiles, the start and end timestamps will be interpreted as
    seconds elapsed from the first recorded event.
    The events that are in progress during these boundaries are not included.
    """

    def get_events_within_time_range(self, start_time_seconds, end_time_seconds):
        start_time_nanoseconds = start_time_seconds * 1000000000
        end_time_nanoseconds = end_time_seconds * 1000000000
        result_events = list()
        for x_event in self._trace_events:
            if (
                start_time_nanoseconds <= x_event.start_time
                and end_time_nanoseconds >= x_event.end_time
            ):
                result_events.append(x_event)
        return result_events

    """
    The TraceEvent class can not support retrieving events based on given datetime objects.
    This is because only smdebug based tracefile store the timestamps based on unix epoch timestamp.
    """

    def get_events_within_range(self, start_time: datetime, end_time: datetime):
        return None

    def get_process_info(self, process_id):
        return self._processes[process_id]

    def get_processes(self):
        return self._processes

    # TODO
    def get_events_for_process(self, pid, start_time, end_time):
        pass

    # TODO
    def get_events_for_thread(self, tid, start_time, end_time):
        pass