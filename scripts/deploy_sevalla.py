"""Deploy an immutable tested branch and verify Sevalla's reported commit."""
import json
import os
import time
import urllib.request
import urllib.error

APP = "847c66d3-0c35-472e-9efb-a3831a059a40"
API = "https://api.sevalla.com/v3/applications/" + APP
REPO = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"]
SHA = os.environ["GITHUB_SHA"]


def request(url, token=None, method="GET", data=None):
    headers = {"Accept": "application/json", "User-Agent": "flight-review-ci"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    github = os.environ["GITHUB_TOKEN"]
    sevalla = os.environ["SEVALLA_API_KEY"]
    if not sevalla:
        raise RuntimeError("SEVALLA_API_KEY secret is missing")
    if request(REPO + "/git/ref/heads/main", github)["object"]["sha"] != SHA:
        print("A newer main revision exists; skip this superseded deployment")
        return
    branch = "deploy/" + SHA
    try:
        request(REPO + "/git/refs", github, "POST", {"ref": "refs/heads/" + branch, "sha": SHA})
    except urllib.error.HTTPError as error:
        if error.code != 422:
            raise
        assert request(REPO + "/git/ref/heads/" + branch, github)["object"]["sha"] == SHA
    variables = request(API + "/env-vars?limit=100", sevalla)["data"]
    existing = next((item for item in variables if item["key"] == "APP_REVISION"), None)
    body = {"key": "APP_REVISION", "value": SHA, "is_runtime": True, "is_buildtime": False}
    request(API + "/env-vars" + ("/" + existing["id"] if existing else ""), sevalla,
            "PUT" if existing else "POST", body)
    deployment = request(API + "/deployments", sevalla, "POST", {"branch": branch})
    print("Deployment:", deployment["id"], flush=True)
    for _ in range(90):
        time.sleep(20)
        deployment = request(API + "/deployments/" + deployment["id"], sevalla)
        print("Status:", deployment["status"], flush=True)
        if deployment["status"] == "success":
            assert deployment["commit_sha"] == SHA, "Wrong deployed revision"
            break
        if deployment["status"] in ("failed", "cancelled", "skipped"):
            raise RuntimeError("Deployment did not succeed")
    else:
        raise TimeoutError("Sevalla rollout timed out")
    for _ in range(12):
        try:
            for path in ("/healthz", "/readyz"):
                health = request("https://review.arkelectron.com" + path)
                assert health["revision"] == SHA
            print("Verified production revision:", SHA)
            return
        except (AssertionError, urllib.error.URLError):
            time.sleep(10)
    raise RuntimeError("Production probes did not confirm the tested revision")


if __name__ == "__main__":
    main()
