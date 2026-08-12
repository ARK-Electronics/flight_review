"""
Tornado handler for AI PID step-response tuning using xAI Grok.
"""
from __future__ import print_function
import json
import os
import sys
import traceback

import tornado.web
import tornado.gen
import tornado.ioloop

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from pid_step_data import collect_pid_step_responses

#pylint: disable=relative-beyond-top-level,invalid-name,line-too-long
from .common import TornadoRequestHandlerBase
from .ai_analysis import (
    _call_grok, _extract_parameters, _extract_flight_summary,
    _checked_log_id, _write_cached_or_empty, _load_ulog_for_analysis,
    _begin_analysis_request, _write_analysis_success, _json_error,
)


PID_TUNING_SYSTEM_PROMPT = """You are an expert PX4 multicopter PID tuner. You analyze step-response
plots produced the same way as PID-Analyzer (Plasmatree): a Wiener deconvolution of setpoint vs
measured rate/attitude reconstructs the average unit-step response.

How to read the curves:
- The target is y = 1.0 (perfect tracking of a unit step).
- Rise / response time: how quickly the curve reaches 1. Faster is better, but not at the
  cost of large overshoot or ringing.
- Overshoot: peak above 1. About 0–10% is typically healthy; >15–20% is usually too aggressive.
- Settling time: time until the curve stays within ±5% (or ±2%) of 1.
- Oscillation crossings: ringing after the rise. Several crossings mean the loop is under-damped.
- Undershoot / final value < 1: the loop is sluggish or P/I is too low.
- High-rate vs low-rate curves (rate loop only): if high-rate (>500 deg/s) is much worse,
  look at D-term and gyro filters (IMU_GYRO_CUTOFF, IMU_DGYRO_CUTOFF).

PX4 parameters:
- Rate (inner) loop: MC_ROLLRATE_P/I/D, MC_PITCHRATE_P/I/D, MC_YAWRATE_P/I/D
- Attitude (outer) loop: MC_ROLL_P, MC_PITCH_P, MC_YAW_P
- Related: IMU_GYRO_CUTOFF, IMU_DGYRO_CUTOFF, MC_*RATE_K (if present)

Typical corrections (change one axis / one gain family at a time, ~10–20%):
- Slow rise, little/no overshoot, final < 1 → increase P (and maybe I if a persistent lag remains).
- Large overshoot or ringing → decrease P, or increase D slightly if the rise is otherwise good.
- Persistent offset after settling → increase I.
- High-frequency noise / D-term chatter (high-rate curve messy) → lower D or lower
  IMU_DGYRO_CUTOFF / IMU_GYRO_CUTOFF carefully.
- Attitude loop should be slower and smoother than the rate loop on the same axis.

Rules:
- Be specific: name the parameter, current value, and a concrete suggested value.
- Explain how the step-response shape justifies each change.
- If a loop looks well tuned, say so and do not invent changes.
- If there are too few steps, the curve is noisy, or data is missing, say the evidence
  is insufficient.
- Never suggest changing many gains at once. Give a short prioritized list.

Format with markdown headers:
1. Overall assessment
2. Per-axis / per-loop findings (rate roll/pitch/yaw, then attitude roll/pitch)
3. Recommended parameter changes (table: parameter, current, suggested, why)
4. What to test on the next flight
"""


def _build_pid_tuning_prompt(step_data, parameters, flight_summary):
    """Build the user prompt for PID step-response tuning analysis."""
    prompt = "# PID Step-Response Tuning Request\n\n"
    prompt += (
        "Analyze the reconstructed step-response curves below (same method as the "
        "Flight Review PID Analysis plots) and recommend PX4 PID / filter changes.\n\n"
    )

    if flight_summary:
        prompt += "## Flight Summary\n```json\n"
        prompt += json.dumps(flight_summary, indent=2, default=str)
        prompt += "\n```\n\n"

    if parameters:
        prompt += "## Current PID / Filter Parameters\n```json\n"
        prompt += json.dumps(parameters, indent=2, default=str)
        prompt += "\n```\n\n"

    if step_data.get('errors'):
        prompt += "## Extraction Notes\n"
        for err in step_data['errors']:
            prompt += "- {}\n".format(err)
        prompt += "\n"

    responses = step_data.get('responses') or []
    if not responses:
        prompt += ("No step-response curves could be computed. Explain what data is "
                   "missing and what the pilot should log or fly to get a useful analysis.\n")
        return prompt

    prompt += "## Step-Response Curves and Metrics\n"
    prompt += (
        "Each `response` series is the average reconstructed unit-step (target = 1.0) "
        "sampled along `time_s`. Use both the metrics and the curve shape.\n\n"
    )
    for item in responses:
        loop = item.get('loop', 'unknown')
        axis = item.get('axis', 'unknown')
        prompt += "### {} {} loop\n".format(axis.capitalize(), loop)
        for key in ('low_rate', 'high_rate'):
            block = item.get(key)
            if not block:
                continue
            prompt += "#### {}\n".format(block.get('label', key))
            prompt += "Metrics:\n```json\n"
            prompt += json.dumps(block.get('metrics', {}), indent=2)
            prompt += "\n```\n"
            prompt += "Curve (time_s, response):\n```json\n"
            prompt += json.dumps({
                'time_s': block.get('time_s', []),
                'response': block.get('response', []),
            }, indent=2)
            prompt += "\n```\n\n"

    return prompt


class PIDAIAnalysisAPIHandler(TornadoRequestHandlerBase):
    """API handler that analyzes PID step-response curves and suggests tuning."""

    @tornado.web.authenticated
    def get(self, *args, **kwargs):
        """GET request - return cached PID analysis if available."""
        log_id = _checked_log_id(self)
        if not log_id:
            return
        _write_cached_or_empty(self, log_id, kind='pid')

    @tornado.web.authenticated
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        """POST request - run PID step-response tuning analysis."""
        log_id, api_key, model = _begin_analysis_request(self)
        if not log_id:
            return

        try:
            ulog, px4_ulog = _load_ulog_for_analysis(log_id)

            # Trace construction is CPU/memory heavy; keep it off the IOLoop.
            step_data = yield tornado.ioloop.IOLoop.current().run_in_executor(
                None, collect_pid_step_responses, ulog)

            flight_summary = _extract_flight_summary(ulog, px4_ulog)
            parameters = {
                k: v for k, v in _extract_parameters(ulog).items()
                if not k.startswith('EKF2_')
            }
            user_prompt = _build_pid_tuning_prompt(
                step_data, parameters, flight_summary)

            ok, payload, status = yield _call_grok(
                api_key, model, PID_TUNING_SYSTEM_PROMPT, user_prompt)
            if ok:
                loops = []
                for item in step_data.get('responses', []):
                    loops.append('{} {}'.format(item.get('axis'), item.get('loop')))
                _write_analysis_success(self, payload, {
                    'duration_s': flight_summary.get('duration_s', 0),
                    'mav_type': flight_summary.get('mav_type', 'Unknown'),
                    'num_parameters': len(parameters),
                    'num_step_responses': len(step_data.get('responses', [])),
                    'has_rate': step_data.get('has_rate', False),
                    'has_attitude': step_data.get('has_attitude', False),
                    'loops': loops,
                    'errors': step_data.get('errors', []),
                }, log_id, kind='pid')
            else:
                _json_error(self, status, payload)

        except Exception as e:  # pylint: disable=broad-except
            traceback.print_exc()
            _json_error(self, 500, 'PID analysis failed: {}'.format(str(e)))
