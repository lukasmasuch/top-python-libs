import os
import re

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

PATH_TO_THIS = os.path.dirname(os.path.abspath(__file__))


# --- Helper: parse GitHub repo owner/repo from a variety of formats ---
def parse_github_repo(repo_str: str) -> str:
    """
    Takes a string that might be one of:
      - owner/repo
      - https://github.com/owner/repo
      - github.com/owner/repo
      - or just a random string (we'll assume it's 'owner/repo' if it matches that pattern)
    Returns 'owner/repo' if successful, else an empty string.
    """
    # If it’s a full URL, try to extract just the portion after github.com/
    if "github.com" in repo_str:
        match = re.search(r"github\.com\/([^/]+\/[^/]+)", repo_str.strip())
        if match:
            return match.group(1).strip()

    # Otherwise, if it looks like "owner/repo"
    if re.match(r"^[A-Za-z0-9_\-\.]+\/[A-Za-z0-9_\-\.]+$", repo_str.strip()):
        return repo_str.strip()

    return ""


# --- Helper: use PyPI to find the GitHub repo for a package name ---
@st.cache_data(ttl=60 * 60 * 24 * 7, show_spinner=False)  # refresh every 7 days
def get_github_repo_from_pypi(pkg_name: str) -> str:
    """
    Uses the PyPI JSON API to fetch project metadata and parse out
    the repository name (owner/repo) from a GitHub URL if available.
    """
    pypi_url = f"https://pypi.org/pypi/{pkg_name}/json"
    try:
        resp = requests.get(pypi_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # "info" section may contain 'home_page' or 'project_urls' with a GitHub link
            info = data.get("info", {})

            # 1) Check project_urls for a 'Source' or 'Github' or 'Homepage' key
            project_urls = info.get("project_urls", {}) or {}
            urls_to_check = []
            urls_to_check.append(info.get("home_page", ""))  # home_page might have GH
            urls_to_check.extend(project_urls.values())  # project_urls might have GH

            for url in urls_to_check:
                if url and "github.com" in url.lower():
                    repo_id = parse_github_repo(url)
                    if repo_id:
                        return repo_id

        # If we fail to get a GH repo from the above, return empty
        return ""
    except Exception:
        return ""


# --- Helper: scrape GitHub's "Dependents" page to retrieve the total number of dependents ---


@st.cache_data(ttl=60 * 60 * 48, show_spinner=False)  # refresh every 48 hours
def get_repo_deps_via_github(github_id: str) -> int:
    """
    Takes an 'owner/repo' string and returns the total number of repositories + packages
    listed on the GitHub dependents page.
    """
    try:
        url = f"https://github.com/{github_id}/network/dependents"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            # If there's an error or no dependents page, return 0
            return 0

        soup = BeautifulSoup(r.text, "html.parser")

        repo_deps = 0
        # Look for the string "<number> Repositories"
        repo_deps_str = soup.find(string=re.compile(r"[0-9,]+\s+Repositories"))
        if repo_deps_str:
            count_search = re.search(r"([0-9,]+)", repo_deps_str, re.IGNORECASE)
            if count_search:
                repo_deps += int(count_search.group(1).replace(",", ""))

        return repo_deps
    except Exception:
        return 0


@st.cache_data(ttl=60 * 60 * 48, show_spinner=False)  # refresh every 48 hours
def get_github_repo_from_librariesio(pkg_name: str) -> str:
    """
    Fallback to the Libraries.io API if PyPI data does not yield a GitHub link.
    Uses the pypi <-> libraries.io project endpoint:
    GET https://libraries.io/api/pypi/<package_name>?api_key=<YOUR_KEY>
    """

    url = f"https://libraries.io/api/pypi/{pkg_name}?api_key={st.secrets.get('LIBRARIES_IO_API_KEY')}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # "repository_url" might have "https://github.com/<owner>/<repo>"
            repo_url = data.get("repository_url", "")
            if repo_url and "github.com" in repo_url.lower():
                repo_id = parse_github_repo(repo_url)
                if repo_id:
                    return repo_id
    except Exception:
        pass

    return ""


def resolve_github_repo_from_package(pkg_name: str) -> str:
    """
    Try to resolve a package name to a GitHub repo:
    1) PyPI JSON API
    2) Fallback to Libraries.io
    Returns '' if nothing found.
    """
    repo_id = get_github_repo_from_pypi(pkg_name)
    if not repo_id and st.secrets.get("LIBRARIES_IO_API_KEY"):
        repo_id = get_github_repo_from_librariesio(pkg_name)
    return repo_id


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)  # refresh every 24 hours
def aggregate_statistics(input_text: str) -> pd.DataFrame:
    # --- 1. Parse the input into a list of strings ---
    # Split by commas, new lines, or whitespace:
    raw_items = re.split(r"[,\n\s]+", input_text.strip())
    items = [x.strip() for x in raw_items if x.strip()]

    added_gh_repos = set()

    results = []

    progress = st.progress(0, text="Aggregating statistics...")

    for i, lib in enumerate(items):
        progress.progress(i / len(items), text="Aggregating statistics...")
        # Attempt to parse as a GitHub repo directly
        gh_repo = parse_github_repo(lib)
        if not gh_repo:
            # If we cannot parse as GH repo, treat 'lib' as a PyPI package name
            gh_repo = resolve_github_repo_from_package(lib)

        if gh_repo in added_gh_repos:
            # Skip since it was already added
            continue

        if gh_repo:
            added_gh_repos.add(gh_repo)
            # Retrieve the number of dependents
            deps_count = get_repo_deps_via_github(gh_repo)
            # Build the relevant links
            github_link = f"https://github.com/{gh_repo}"
            dependents_link = f"{github_link}/network/dependents"
        else:
            # If we absolutely cannot find the GH repo, skip or put placeholders
            deps_count = None
            github_link = None
            dependents_link = None

        # Add row to results
        results.append(
            {
                "Name": lib,
                "Number of dependents": deps_count,
                "GitHub Repo": github_link,
                "Dependents Page": dependents_link,
            }
        )

    progress.empty()

    # --- 2. Put data into a DataFrame, sort by # of dependents ---
    df = pd.DataFrame(results)
    df = df.sort_values(by="Number of dependents", ascending=False).reset_index(
        drop=True
    )
    return df


st.subheader("Python libraries with most dependents on GitHub")
st.caption(
    "Dependents is a number provided by Github that refers to the number of public "
    "repositories that declares a library as a dependency on Github. This number does "
    "not include private repositories or transitive dependencies."
)

with open(os.path.join(PATH_TO_THIS, "python-libs.txt"), "r") as f:
    local_list = f.read()

input_text = local_list

if "edit" in st.query_params:
    with st.form("python_libraries"):
        input_text = st.text_area(
            "Enter Python libraries (PyPI names) or GitHub repos (owner/repo) separated by "
            "commas, whitespace or new lines.",
            value=local_list,
        )
        st.form_submit_button("Aggregate Statistics")

df = aggregate_statistics(input_text)

st.dataframe(
    df,
    use_container_width=True,
    column_config={
        "GitHub Repo": st.column_config.LinkColumn(
            display_text=r"https://github\.com/(.*)",
        ),
        "Dependents Page": st.column_config.LinkColumn(
            display_text="Open",
        ),
    },
)
