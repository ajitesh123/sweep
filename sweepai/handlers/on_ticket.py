"""
on_ticket is the main function that is called when a new issue is created.
It is only called by the webhook handler in sweepai/api.py.
"""

import copy
import os
import traceback
from time import time
from rich import print as rprint
from time import sleep

from github import BadCredentialsException
from github.PullRequest import PullRequest as GithubPullRequest
from loguru import logger


from sweepai.chat.api import posthog_trace
from sweepai.agents.image_description_bot import ImageDescriptionBot
from sweepai.config.client import (
    RESET_FILE,
    REVERT_CHANGED_FILES_TITLE,
    SweepConfig,
)
from sweepai.config.server import (
    ENV,
    GITHUB_LABEL_NAME,
    IS_SELF_HOSTED,
    MONGODB_URI,
)
from sweepai.core.entities import (
    MockPR,
    NoFilesException,
    SweepPullRequest,
    render_fcrs,
)
from sweepai.core.pr_reader import PRReader
from sweepai.core.pull_request_bot import PRSummaryBot
from sweepai.core.sweep_bot import get_files_to_change
from sweepai.handlers.on_failing_github_actions import on_failing_github_actions
from sweepai.handlers.create_pr import (
    handle_file_change_requests,
)
from sweepai.utils.concurrency_utils import fire_and_forget_wrapper
from sweepai.utils.image_utils import get_image_contents_from_urls, get_image_urls_from_issue
from sweepai.utils.issue_validator import validate_issue
from sweepai.utils.prompt_constructor import get_issue_request
from sweepai.utils.ticket_rendering_utils import add_emoji, center, process_summary, remove_emoji, get_payment_messages, get_comment_header, send_email_to_user, raise_on_no_file_change_requests, handle_empty_repository, delete_old_prs
from sweepai.utils.validate_license import validate_license
from sweepai.utils.buttons import Button, ButtonList
from sweepai.utils.chat_logger import ChatLogger
from sentry_sdk import set_user
from sweepai.utils.event_logger import posthog
from sweepai.utils.github_utils import (
    CURRENT_USERNAME,
    ClonedRepo,
    commit_multi_file_changes,
    convert_pr_draft_field,
    create_branch,
    get_github_client,
    refresh_token,
    sanitize_string_for_github,
    validate_and_sanitize_multi_file_changes,
)
from sweepai.utils.slack_utils import add_slack_context
from sweepai.utils.str_utils import (
    BOT_SUFFIX,
    FASTER_MODEL_MESSAGE,
    blockquote,
    bold,
    bot_suffix,
    create_collapsible,
    discord_suffix,
    get_hash,
    strip_sweep,
    to_branch_name
)
from sweepai.utils.ticket_utils import (
    fetch_relevant_files,
)

@posthog_trace
def on_ticket(
    username: str,
    title: str,
    summary: str,
    issue_number: int,
    issue_url: str, # purely for logging purposes
    repo_full_name: str,
    repo_description: str,
    installation_id: int,
    comment_id: int = None,
    edited: bool = False,
    tracking_id: str | None = None,
):
    set_user({"username": username})
    logger.info(f"Logging with tracking ID: {tracking_id}")
    if tracking_id is None:
        tracking_id = get_hash()

    on_ticket_start_time = time()
    
    #Does some regex based on user input after "Sweep" in title to indentify the mode
    title, slow_mode, do_map, subissues_mode, sandbox_mode, fast_mode, lint_mode = strip_sweep(title)

    #one aspect here is that summary is cleaned up to remove HTML tags, markdown formatted checklists, etc.
    summary, repo_name, user_token, g, repo, current_issue, assignee, overrided_branch_name = process_summary(summary, issue_number, repo_full_name, installation_id)

    chat_logger = ChatLogger({
    "repo_name": repo_name, "title": title, "summary": summary, "issue_number": issue_number,
    "issue_url": issue_url, "username": username if not username.startswith("sweep") else assignee,
    "repo_full_name": repo_full_name, "repo_description": repo_description, "installation_id": installation_id,
    "type": "ticket", "mode": ENV, "comment_id": comment_id, "edited": edited, "tracking_id": tracking_id
    }, active=True) if MONGODB_URI else None

    modify_files_dict_history: list[dict[str, dict[str, str]]] = []

    #Simplify the logic to say paying user and using faster model
    is_paying_user = True
    use_faster_model = False

    if not comment_id and not edited and chat_logger and not sandbox_mode:
        fire_and_forget_wrapper(chat_logger.add_successful_ticket)(
            gpt3=use_faster_model
        )

    organization, repo_name = repo_full_name.split("/")
    metadata = {
        "issue_url": issue_url,
        "repo_full_name": repo_full_name,
        "organization": organization,
        "repo_name": repo_name,
        "repo_description": repo_description,
        "username": username,
        "comment_id": comment_id,
        "title": title,
        "installation_id": installation_id,
        "function": "on_ticket",
        "edited": edited,
        "model": "gpt-3.5" if use_faster_model else "gpt-4",
        "tier": "pro" if is_paying_user else "free",
        "mode": ENV,
        "slow_mode": slow_mode,
        "do_map": do_map,
        "subissues_mode": subissues_mode,
        "sandbox_mode": sandbox_mode,
        "fast_mode": fast_mode,
        "is_self_hosted": IS_SELF_HOSTED,
        "tracking_id": tracking_id,
    }

    try:
        if current_issue.state == "closed":
            return {"success": False, "reason": "Issue is closed"}

        replies_text = ""
        summary = summary if summary else ""
        issue_comment = None

        config_pr_url = None
        cloned_repo: ClonedRepo = ClonedRepo(
            repo_full_name,
            installation_id=installation_id,
            token=user_token,
            repo=repo,
            branch=overrided_branch_name,
        )

        internal_message_summary = summary
        internal_message_summary += add_slack_context(internal_message_summary)
        error_message = validate_issue(title + internal_message_summary)
        if error_message:
            logger.warning(f"Validation error: {error_message}")

            posthog.capture(
                username,
                "invalid_issue",
                properties={
                    **metadata,
                    "duration": round(time() - on_ticket_start_time),
                },
            )
            return {"success": True}

        prs_extracted = PRReader.extract_prs(repo, summary)
        if prs_extracted:
            internal_message_summary += "\n\n" + prs_extracted

        try:
            # search/context manager
            logger.info("Searching for relevant snippets...")
            # fetch images from body of issue
            image_urls = get_image_urls_from_issue(issue_number, repo_full_name, installation_id)
            image_contents = get_image_contents_from_urls(image_urls)
            if image_contents: # doing it here to avoid editing the original issue
                internal_message_summary += ImageDescriptionBot().describe_images(text=title + internal_message_summary, images=image_contents)
            print(f"Image Contents: {image_contents}")
            _user_token, g = get_github_client(installation_id)
            user_token, g, repo = refresh_token(repo_full_name, installation_id)
            cloned_repo.token = user_token
            repo = g.get_repo(repo_full_name)

            newline = "\n"
            for message, repo_context_manager in fetch_relevant_files.stream(
                cloned_repo,
                title,
                internal_message_summary,
                replies_text,
                username,
                metadata,
                on_ticket_start_time,
                tracking_id,
                is_paying_user,
                issue_url,
                chat_logger,
                images=image_contents
            ):
                if repo_context_manager.current_top_snippets + repo_context_manager.read_only_snippets:
                    pass

            cloned_repo = repo_context_manager.cloned_repo
            user_token, g, repo = refresh_token(repo_full_name, installation_id)
        except Exception as e:
            raise e
        
        # Fetch git commit history
        if not repo_description:
            repo_description = "No description provided."

        internal_message_summary += replies_text
        issue_request = get_issue_request(title, internal_message_summary)

        try:
            newline = "\n"
            logger.info("Fetching files to modify/create...")
            for renames_dict, user_facing_message, file_change_requests in get_files_to_change.stream(
                relevant_snippets=repo_context_manager.current_top_snippets,
                read_only_snippets=repo_context_manager.read_only_snippets,
                problem_statement=f"{title}\n\n{internal_message_summary}",
                repo_name=repo_full_name,
                cloned_repo=cloned_repo,
                images=image_contents,
                chat_logger=chat_logger
            ):
                planning_markdown = render_fcrs(file_change_requests)
                print(f"=======================================")
                print(f"planning_markdown: {planning_markdown}")
                print(f"=======================================")
                edit_sweep_comment=""
            raise_on_no_file_change_requests(title, summary, edit_sweep_comment, file_change_requests, renames_dict)
        except Exception as e:
            logger.exception(f"Error occured in running get_filesOt-_change: {e}")
            # title and summary are defined elsewhere
            raise e

        # VALIDATION (modify)
        try:
            # edit_sweep_comment(
            #     "I'm currently validating your changes using parsers and linters to check for mistakes like syntax errors or undefined variables. If I see any of these errors, I will automatically fix them.",
            #     3,
            # )
            pull_request: SweepPullRequest = SweepPullRequest(
                title="Sweep: " + title,
                branch_name="sweep/" + to_branch_name(title),
                content="",
            )
            logger.info("Making PR...")
            pull_request.branch_name = create_branch(
                cloned_repo.repo, pull_request.branch_name, base_branch=overrided_branch_name
            )
            modify_files_dict, changed_file, file_change_requests = handle_file_change_requests(
                file_change_requests=file_change_requests,
                request=issue_request,
                cloned_repo=cloned_repo,
                username=username,
                installation_id=installation_id,
                renames_dict=renames_dict
            )
            pull_request_bot = PRSummaryBot()
            commit_message = pull_request_bot.get_commit_message(modify_files_dict, renames_dict=renames_dict, chat_logger=chat_logger)[:50]
            modify_files_dict_history.append(copy.deepcopy(modify_files_dict))
            new_file_contents_to_commit = {file_path: file_data["contents"] for file_path, file_data in modify_files_dict.items()}
            previous_file_contents_to_commit = copy.deepcopy(new_file_contents_to_commit)
            new_file_contents_to_commit, files_removed = validate_and_sanitize_multi_file_changes(cloned_repo.repo, new_file_contents_to_commit, file_change_requests)
            if files_removed and username:
                posthog.capture(
                    username,
                    "polluted_commits_error",
                    properties={
                        "old_keys": ",".join(previous_file_contents_to_commit.keys()),
                        "new_keys": ",".join(new_file_contents_to_commit.keys()) 
                    },
                )
            commit = commit_multi_file_changes(cloned_repo, new_file_contents_to_commit, commit_message, pull_request.branch_name, renames_dict=renames_dict)
            # edit_sweep_comment(
            #     f"Your changes have been successfully made to the branch [`{pull_request.branch_name}`](https://github.com/{repo_full_name}/tree/{pull_request.branch_name}). I have validated these changes using a syntax checker and a linter.",
            #     3,
            # )
        except Exception as e:
            logger.exception(e)
            raise e
        else:
            try:
                fire_and_forget_wrapper(remove_emoji)(content_to_delete="eyes")
                fire_and_forget_wrapper(add_emoji)("rocket")
            except Exception as e:
                logger.error(e)

        # set all fcrs without a corresponding change to be failed
        for file_change_request in file_change_requests:
            if file_change_request.status != "succeeded":
                file_change_request.status = "failed"
            # also update all commit hashes associated with the fcr
            file_change_request.commit_hash_url = commit.html_url if commit else None
        if not file_change_requests:
            raise NoFilesException()
        changed_files = []

        # append all files that have been changed
        if modify_files_dict:
            for file_name, _ in modify_files_dict.items():
                changed_files.append(file_name)

        # Refresh token
        current_issue = refresh_issue_token(repo, issue_number, cloned_repo, repo_full_name, installation_id)

        pr = create_and_manage_pull_request(
                                            modify_files_dict,
                                            title,
                                            internal_message_summary,
                                            issue_number,
                                            repo,
                                            repo_full_name,
                                            overrided_branch_name,
                                            pull_request,
                                            changed_files,
                                            username,
                                            current_issue,
                                            replies_text,
                                            user_token,
                                            installation_id,
                                            chat_logger
                                        )

    except Exception as e:
        posthog.capture(
            username,
            "failed",
            properties={
                **metadata,
                "error": str(e),
                "trace": traceback.format_exc(),
                "duration": round(time() - on_ticket_start_time),
            },
        )
        raise e
    posthog.capture(
        username,
        "success",
        properties={**metadata, "duration": round(time() - on_ticket_start_time)},
    )
    logger.info("on_ticket success in " + str(round(time() - on_ticket_start_time)))
    return {"success": True}


def refresh_issue_token(repo, issue_number, cloned_repo, repo_full_name, installation_id):
    """
    Attempts to retrieve an issue from the repository and refreshes the token if a BadCredentialsException is encountered.

    :param repo: The repository object from which to retrieve the issue.
    :param issue_number: The number of the issue to retrieve.
    :param cloned_repo: The cloned repository object whose token may need updating.
    :param repo_full_name: The full name of the repository, used to refresh the token.
    :param installation_id: The installation ID used for authentication.
    :return: The current issue object after ensuring valid credentials.
    """
    try:
        return repo.get_issue(number=issue_number)
    except BadCredentialsException:
        # Refresh the authentication token and update the repository information
        user_token, g, updated_repo = refresh_token(repo_full_name, installation_id)
        cloned_repo.token = user_token
        return updated_repo.get_issue(number=issue_number)

def create_and_manage_pull_request(
    modify_files_dict,
    title,
    internal_message_summary,
    issue_number,
    repo,
    repo_full_name,
    overrided_branch_name,
    pull_request,
    changed_files,
    username,
    current_issue,
    replies_text,
    user_token,
    installation_id,
    chat_logger 
):
    pr_changes = MockPR(
        file_count=len(modify_files_dict),
        title=pull_request.title,
        body="",  # overrided later
        pr_head=pull_request.branch_name,
        base=repo.get_branch(SweepConfig.get_branch(repo)).commit,
        head=repo.get_branch(pull_request.branch_name).commit,
    )
    pr_changes = PRSummaryBot.get_pull_request_summary(
        title + "\n" + internal_message_summary,
        issue_number,
        repo,
        overrided_branch_name,
        pull_request,
        pr_changes
    )

    change_location = f" [`{pr_changes.pr_head}`](https://github.com/{repo_full_name}/commits/{pr_changes.pr_head}).\n\n"
    review_message = "Here are my self-reviews of my changes at" + change_location

    pr: GithubPullRequest = repo.create_pull(
        title=pr_changes.title,
        body=pr_changes.body,
        head=pr_changes.pr_head,
        base=overrided_branch_name or SweepConfig.get_branch(repo),
        draft=False,
    )

    try:
        pr.add_to_assignees(username)
    except Exception as e:
        logger.warning(f"Failed to add assignee {username}: {e}, probably a bot.")

    if len(changed_files) > 1:
        revert_buttons = [Button(label=f"{RESET_FILE} {changed_file}") for changed_file in set(changed_files)]
        revert_buttons_list = ButtonList(buttons=revert_buttons, title=REVERT_CHANGED_FILES_TITLE)

        if revert_buttons:
            pr.create_issue_comment(revert_buttons_list.serialize() + BOT_SUFFIX)

    pr.add_to_labels(GITHUB_LABEL_NAME)
    current_issue.create_reaction("rocket")
    heres_pr_message = f'<h1 align="center">🚀 Here\'s the PR! <a href="{pr.html_url}">#{pr.number}</a></h1>'
    progress_message = ''
    # edit_sweep_comment(
    #     review_message + "\n\nSuccess! 🚀",
    #     4,
    #     pr_message=(
    #         f"{center(heres_pr_message)}\n{center(progress_message)}\n{center(payment_message_start)}"
    #     ),
    #     done=True,
    # )

    on_failing_github_actions(
        f"{title}\n{internal_message_summary}\n{replies_text}",
        repo,
        username,
        pr,
        user_token,
        installation_id,
        chat_logger=chat_logger
    )

    convert_pr_draft_field(pr, is_draft=False, installation_id=installation_id)

    return pr