# Copyright 2015 Cloudbase Solutions Srl
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import argparse
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml


class RefNotFoundError(Exception):
    pass


def get_remote_git_ref(remote_url, branch):
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "--quiet", "--refs", remote_url, branch],
        capture_output=True,
        check=True,
        env={"GIT_TERMINAL_PROMPT": "0"},
        text=True,
    )

    nrefs = result.stdout.count("\n")
    if nrefs == 1:
        return result.stdout.split("\t")[0]
    if nrefs == 0:
        raise RefNotFoundError("No ref found")
    raise RefNotFoundError("More than one ref found")


def check_db(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs(
            job_name TEXT,
            remote_url TEXT,
            branch TEXT,
            last_ref TEXT NOT NULL,
            PRIMARY KEY (job_name, remote_url, branch))
        """)


def get_last_git_ref(db_path, job_name, remote_url, branch):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_ref FROM jobs WHERE job_name=? AND remote_url=? AND branch=?",
            (job_name, remote_url, branch)
        ).fetchone()
        return row[0] if row else None


def set_last_git_ref(db_path, job_name, remote_url, branch, curr_ref):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO jobs (job_name, remote_url, branch, last_ref)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (job_name, remote_url, branch)
            DO UPDATE SET last_ref=excluded.last_ref
        """, (job_name, remote_url, branch, curr_ref))


def exec_action_cmd(action_cmd):
    print(f"Executing action: {action_cmd}")
    # pylint: disable-next=subprocess-run-check
    result = subprocess.run(action_cmd, shell=True)
    result.check_returncode()


def exec_action_url(action_url):
    print(f"Executing action: {action_url}")
    response = requests.get(action_url, timeout=10)
    response.raise_for_status()


def process_job(db_path, job_name, job_config):
    run_action = True
    action_cmd = job_config.get("action_cmd")
    action_url = job_config.get("action_url")
    if (action_cmd and action_url) or not (action_cmd or action_url):
        raise ValueError("Either action_cmd or action_url is required for a job")

    for repo in job_config.get("repos", []):
        remote_url = repo.get("remote_url")
        if not remote_url:
            raise ValueError("remote_url is required for a repository")
        branch = repo.get("branch", "master")

        try:
            curr_ref = get_remote_git_ref(remote_url, branch)
        except RefNotFoundError as ex:
            print(
                f"Latest commit unknown for branch {branch} in repo {remote_url}: {ex}"
            )
            continue

        prev_ref = get_last_git_ref(db_path, job_name, remote_url, branch)

        print(f"Repo url: {remote_url}")
        print(f"Curr ref: {curr_ref}")
        print(f"Prev ref: {prev_ref}")

        if curr_ref != prev_ref:
            if run_action:
                if action_cmd:
                    exec_action_cmd(action_cmd)
                else:
                    exec_action_url(action_url)
                run_action = False
            set_last_git_ref(db_path, job_name, remote_url, branch, curr_ref)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Polls git repositories and runs actions on updates."
    )
    parser.add_argument(
        "config_file", metavar="CONFIGFILE", help="Path to the YAML config file"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config_path = Path(args.config_file).expanduser()
    with config_path.open("rb") as stream:
        config = yaml.safe_load(stream)

    db_path = Path(config.get("db", {}).get("path", "gitpoll.s3db")).expanduser()
    check_db(db_path)

    jobs = config.get("jobs", {})
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(process_job, db_path, job_name, jobs[job_name]): job_name
            for job_name in jobs
        }
        for future in as_completed(futures):
            job_name = futures[future]
            ex = future.exception()
            if ex:
                print(f"Job {job_name} failed: {ex}")
            else:
                print(f"Job {job_name} finished")


if __name__ == "__main__":
    main()
