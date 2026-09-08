# pylint: disable=invalid-name,line-too-long
"""One isolated analysis job. Results are committed atomically to the durable queue."""
import asyncio
import json
import os
import sys
import time

from isolated_worker import limit_resources


async def analyze(job):
    """Extract data and call the model outside the web process."""
    from tornado_handlers import ai_analysis as ai  # pylint: disable=import-outside-toplevel
    from tornado_handlers import ai_chat as chat  # pylint: disable=import-outside-toplevel
    from tornado_handlers import pid_ai_analysis as pid  # pylint: disable=import-outside-toplevel
    from config import get_xai_api_key  # pylint: disable=import-outside-toplevel
    from pid_step_data import collect_pid_step_responses  # pylint: disable=import-outside-toplevel
    options = json.loads(job['Request'])
    log_id, kind = job['LogId'], job['Kind']
    extra = None
    summary = {}
    if kind == 'pid':
        ulog, px4 = ai._load_ulog_for_analysis(log_id)  # pylint: disable=protected-access
        steps = collect_pid_step_responses(ulog)
        flight = ai._extract_flight_summary(ulog, px4)  # pylint: disable=protected-access
        params = {k: v for k, v in ai._extract_parameters(ulog).items()  # pylint: disable=protected-access
                  if not k.startswith('EKF2_')}
        prompt = pid._build_pid_tuning_prompt(steps, params, flight)  # pylint: disable=protected-access
        system = pid.PID_TUNING_SYSTEM_PROMPT
        summary = dict(flight, num_parameters=len(params), num_step_responses=len(steps['responses']),
                       has_rate=steps.get('has_rate', False),
                       has_attitude=steps.get('has_attitude', False),
                       loops=[str(item.get('axis')) + ' ' + str(item.get('loop'))
                              for item in steps['responses']], errors=steps.get('errors', []))
    else:
        data = ai._extracted_flight_data(log_id)  # pylint: disable=protected-access
        prompt = ai._build_analysis_prompt(  # pylint: disable=protected-access
            data['flight_summary'], data['pid_data'], data['ekf_data'], data['vehicle_status'],
            data['parameters'], data['logged_messages'], data['motor_failure'], for_chat=kind == 'chat')
        system = ai.SYSTEM_PROMPT
        summary = dict(data['flight_summary'], num_parameters=len(data['parameters']),
                       has_ekf_data=bool(data['ekf_data']), has_pid_data=bool(data['pid_data']),
                       num_messages=len(data['logged_messages']))
        if kind == 'chat':
            history = chat._chat_history_payload(log_id, job['Username'])['messages']  # pylint: disable=protected-access
            history = history[-22:] + [{'role': 'user', 'content': options['message']}]
            extra = [{'role': 'user', 'content': prompt},
                     {'role': 'assistant', 'content': chat._CHAT_CONTEXT_ACK}] + history  # pylint: disable=protected-access
            system = chat.CHAT_SYSTEM_PROMPT
    ok, result, _status = await ai._call_grok(  # pylint: disable=protected-access
        get_xai_api_key(), options['model'], system, prompt,
        effort=options['effort'], extra_messages=extra)
    if not ok:
        raise RuntimeError('Model service failed; retry later')
    result['effort'] = options['effort']
    if kind == 'chat':
        history.append({'role': 'assistant', 'content': result['analysis']})
        result.update(reply=result['analysis'], messages=history)
    else:
        result['data_summary'] = summary
    return result


async def main(job_id):
    """Recheck authorization at execution time and never publish cancelled results."""
    from tornado_handlers.analysis_jobs import connection  # pylint: disable=import-outside-toplevel
    from tornado_handlers.ai_analysis import _save_cached_analysis  # pylint: disable=import-outside-toplevel
    from tornado_handlers.ai_chat import chat_kind  # pylint: disable=import-outside-toplevel
    with connection() as con:
        job = con.execute("SELECT * FROM AnalysisJobs WHERE Id=? AND State='running'", (job_id,)).fetchone()
        if not job:
            return
        user = con.execute('SELECT Approved, IsAdmin FROM Users WHERE Username=?', (job['Username'],)).fetchone()
        log = con.execute('SELECT Public, Uploader FROM Logs WHERE Id=?', (job['LogId'],)).fetchone()
    try:
        if not user or not user[0] or not log or not (log[0] or user[1] or log[1] == job['Username']):
            raise RuntimeError('Access is no longer available')
        result = await analyze(job)
        with connection() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT State FROM AnalysisJobs WHERE Id=?', (job_id,)).fetchone()[0] != 'running':
                return
            kind = chat_kind(job['Username']) if job['Kind'] == 'chat' else job['Kind']
            _save_cached_analysis(job['LogId'], result, kind=kind)
            con.execute("UPDATE AnalysisJobs SET State='done', Result=?, Updated=? WHERE Id=?",
                        (json.dumps(result), time.time(), job_id))
    except Exception:  # pylint: disable=broad-except
        with connection() as con:
            con.execute("UPDATE AnalysisJobs SET State='failed', Error='Analysis failed; retry later', "
                        "Updated=? WHERE Id=? AND State='running'", (time.time(), job_id))
        raise


if __name__ == '__main__':
    limit_resources(int(os.environ.get('AI_WORKER_MEMORY_MB', '2048')))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'plot_app'))
    asyncio.run(main(sys.argv[1]))
