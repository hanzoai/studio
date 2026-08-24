"""
Prometheus-compatible metrics endpoint for Hanzo Studio.

Exposes /metrics in Prometheus text format for VictoriaMetrics scraping.
Tracks prompt execution, WebSocket connections, and HTTP requests.
"""
import time as _time
import threading

_lock = threading.Lock()

# Counters
_prompts_total = 0
_prompts_success = 0
_prompts_error = 0
_http_requests_total: dict[str, int] = {}  # method:status -> count

# Gauges
_prompts_active = 0
_websocket_connections = 0

# Histogram buckets for prompt execution time (seconds)
_DURATION_BUCKETS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
_duration_bucket_counts = [0] * len(_DURATION_BUCKETS)
_duration_sum = 0.0
_duration_count = 0

# Start time for uptime
_start_time = _time.monotonic()


def record_prompt_start():
    global _prompts_active
    with _lock:
        _prompts_active += 1


def record_prompt_end(duration_seconds: float, success: bool = True):
    global _prompts_total, _prompts_success, _prompts_error, _prompts_active
    global _duration_sum, _duration_count
    with _lock:
        _prompts_total += 1
        _prompts_active = max(0, _prompts_active - 1)
        if success:
            _prompts_success += 1
        else:
            _prompts_error += 1

        _duration_sum += duration_seconds
        _duration_count += 1
        for i, bucket in enumerate(_DURATION_BUCKETS):
            if duration_seconds <= bucket:
                _duration_bucket_counts[i] += 1


def set_websocket_connections(count: int):
    global _websocket_connections
    _websocket_connections = count


def record_http_request(method: str, status: int):
    key = f"{method}:{status}"
    with _lock:
        _http_requests_total[key] = _http_requests_total.get(key, 0) + 1


def render_metrics() -> str:
    """Render all metrics in Prometheus text exposition format."""
    lines = []

    with _lock:
        # Prompt counters
        lines.append("# HELP studio_prompts_total Total number of prompts processed")
        lines.append("# TYPE studio_prompts_total counter")
        lines.append(f"studio_prompts_total {_prompts_total}")

        lines.append("# HELP studio_prompts_success_total Total successful prompts")
        lines.append("# TYPE studio_prompts_success_total counter")
        lines.append(f"studio_prompts_success_total {_prompts_success}")

        lines.append("# HELP studio_prompts_error_total Total failed prompts")
        lines.append("# TYPE studio_prompts_error_total counter")
        lines.append(f"studio_prompts_error_total {_prompts_error}")

        # Gauges
        lines.append("# HELP studio_prompts_active Currently executing prompts")
        lines.append("# TYPE studio_prompts_active gauge")
        lines.append(f"studio_prompts_active {_prompts_active}")

        lines.append("# HELP studio_websocket_connections Active WebSocket connections")
        lines.append("# TYPE studio_websocket_connections gauge")
        lines.append(f"studio_websocket_connections {_websocket_connections}")

        # Uptime
        lines.append("# HELP studio_uptime_seconds Seconds since server start")
        lines.append("# TYPE studio_uptime_seconds gauge")
        lines.append(f"studio_uptime_seconds {_time.monotonic() - _start_time:.1f}")

        # Duration histogram
        lines.append("# HELP studio_prompt_duration_seconds Prompt execution time")
        lines.append("# TYPE studio_prompt_duration_seconds histogram")
        cumulative = 0
        for i, bucket in enumerate(_DURATION_BUCKETS):
            cumulative += _duration_bucket_counts[i]
            lines.append(f'studio_prompt_duration_seconds_bucket{{le="{bucket}"}} {cumulative}')
        lines.append(f'studio_prompt_duration_seconds_bucket{{le="+Inf"}} {_duration_count}')
        lines.append(f"studio_prompt_duration_seconds_sum {_duration_sum:.3f}")
        lines.append(f"studio_prompt_duration_seconds_count {_duration_count}")

        # HTTP request counters
        if _http_requests_total:
            lines.append("# HELP studio_http_requests_total HTTP requests by method and status")
            lines.append("# TYPE studio_http_requests_total counter")
            for key, count in sorted(_http_requests_total.items()):
                method, status = key.split(":")
                lines.append(f'studio_http_requests_total{{method="{method}",status="{status}"}} {count}')

    lines.append("")
    return "\n".join(lines)
