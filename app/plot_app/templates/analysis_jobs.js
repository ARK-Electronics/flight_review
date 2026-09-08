// Jobs survive page reloads. Store only their opaque ids in session storage.
async function flightAnalysisRequest(url, options) {
    const key = 'flight-analysis:' + url;
    let jobId = sessionStorage.getItem(key);
    if (!jobId) {
        const response = await fetch(url, options);
        if (response.status !== 202) return response;
        jobId = (await response.json()).job_id;
        sessionStorage.setItem(key, jobId);
    }
    const jobUrl = '/ai_analysis/jobs/' + encodeURIComponent(jobId);
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn-outline-secondary';
    cancel.textContent = 'Cancel analysis';
    cancel.onclick = async () => { await fetch(jobUrl, {method: 'DELETE'}); };
    const container = document.getElementById('pid-ai-panel') || document.querySelector('main') || document.body;
    container.appendChild(cancel);
    try {
        while (true) {
            const poll = await fetch(jobUrl, {cache: 'no-store'});
            if (!poll.ok) {
                if (poll.status === 404) sessionStorage.removeItem(key);
                return poll;
            }
            const job = await poll.json();
            if (job.state === 'done') {
                sessionStorage.removeItem(key);
                return new Response(JSON.stringify(job.result), {status: 200});
            }
            if (['failed', 'cancelled'].includes(job.state)) {
                sessionStorage.removeItem(key);
                return new Response(JSON.stringify({error: job.error || 'Analysis cancelled'}), {status: 400});
            }
            await new Promise(resolve => setTimeout(resolve, 5000));
        }
    } finally {
        cancel.remove();
    }
}
