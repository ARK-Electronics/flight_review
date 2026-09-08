"""
Tornado handler for AI PID step-response tuning using xAI Grok.
"""
from __future__ import print_function
import json
import os
import sys

import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))

#pylint: disable=relative-beyond-top-level,invalid-name,line-too-long
from .common import TornadoRequestHandlerBase
from .ai_analysis import _checked_log_id, _write_cached_or_empty


PID_TUNING_SYSTEM_PROMPT = """You are an expert PX4 multicopter PID tuner. You analyze step-response
plots produced the same way as PID-Analyzer (Plasmatree): a Wiener deconvolution of setpoint vs
measured rate/attitude reconstructs the average unit-step response.

How to read the curves:
- The target is y = 1.0 (perfect tracking of a unit step).
- Rise / response time: how quickly the curve reaches 1. Faster is better, but not at the
  cost of large overshoot or ringing.
- Overshoot: peak above 1. About 0â€“10% is typically healthy; >15â€“20% is usually too aggressive.
- Settling time: time until the curve stays within Â±5% (or Â±2%) of 1.
- Oscillation crossings: ringing after the rise. Several crossings mean the loop is under-damped.
- Undershoot / final value < 1: the loop is sluggish or P/I is too low.
- High-rate vs low-rate curves (rate loop only): if high-rate (>500 deg/s) is much worse,
  look at D-term and gyro filters (IMU_GYRO_CUTOFF, IMU_DGYRO_CUTOFF).

PX4 parameters:
- Rate (inner) loop: MC_ROLLRATE_P/I/D, MC_PITCHRATE_P/I/D, MC_YAWRATE_P/I/D
- Attitude (outer) loop: MC_ROLL_P, MC_PITCH_P, MC_YAW_P
- Related: IMU_GYRO_CUTOFF, IMU_DGYRO_CUTOFF, MC_*RATE_K (if present)

Typical corrections (change one axis / one gain family at a time, ~10â€“20%):
- Slow rise, little/no overshoot, final < 1 â†’ increase P (and maybe I if a persistent lag remains).
- Large overshoot or ringing â†’ decrease P, or increase D slightly if the rise is otherwise good.
- Persistent offset after settling â†’ increase I.
- High-frequency noise / D-term chatter (high-rate curve messy) â†’ lower D or lower
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
    def post(self, *args, **kwargs):
        """Submit isolated PID computation and model analysis."""
        from .analysis_jobs import submit_analysis  # pylint: disable=import-outside-toplevel
        submit_analysis(self, 'pid')
