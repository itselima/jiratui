# Using JiraTUI through the Atlassian MCP server (no API token)

This fork can talk to Jira Cloud through the [Atlassian Remote MCP server](https://mcp.atlassian.com/v1/mcp)
instead of the REST API. You log in once with your Atlassian account in the browser (OAuth); no API token is needed.

## Setup

1. Install the fork (Python 3.12+):

   ```shell
   uv tool install git+https://github.com/itselima/jiratui@mcp-transport
   # or from a local checkout: python3.14 -m venv .venv && .venv/bin/pip install -e .
   ```

2. Create `~/.config/jiratui/config.yaml`:

   ```yaml
   use_mcp: true
   jira_api_base_url: https://<your-site>.atlassian.net
   jira_base_url: https://<your-site>.atlassian.net
   # optional
   default_project_key_or_id: ABC
   ```

   `jira_api_username` and `jira_api_token` are not needed.

3. Log in once:

   ```shell
   jiratui mcp login
   ```

   This registers JiraTUI as an OAuth client of the MCP server, opens the browser so you can approve access, and stores
   the tokens in `~/.config/jiratui/mcp-oauth.json` (mode 600). Tokens are refreshed automatically.
   `jiratui mcp logout` deletes them.

4. Run `jiratui ui` as usual.

## What works

Searching (filters and JQL), viewing work items, editing fields, changing status, comments (add/edit), work logs
(add/edit), creating work items and subtasks, linking work items, remote links (read-only), user lookups.

## What does not (the MCP server has no tools for these)

- Attachments (upload, download, delete)
- Deleting work items, comments, work logs and links; creating or deleting remote (web) links
- Boards and sprints: the sprint picker is empty. The "Active Sprint" filter still works, since it is plain JQL.
- JQL autocomplete and favourite filters
- Setting the remaining estimate when logging work

Some data is derived instead of read directly, because the MCP server has no endpoint for it:

- **Statuses of a project**: statuses seen on the 100 most recently updated work items of the project.
- **Assignable users without a search term**: assignees/reporters of recently updated work items, plus you. Typing a
  name searches all users (the MCP server returns up to ~5 matches per search).
- **Fields list**: read from a recently updated work item.

## Options

| Setting                   | Default                           | Purpose                                                      |
|---------------------------|-----------------------------------|--------------------------------------------------------------|
| `use_mcp`                 | `false`                           | Route all Jira calls through the MCP server                  |
| `mcp_server_url`          | `https://mcp.atlassian.com/v1/mcp` | MCP endpoint                                                 |
| `mcp_oauth_callback_port` | `33418`                           | Local port that receives the OAuth redirect                  |
| `mcp_cloud_id`            | hostname of `jira_api_base_url`   | Site hostname or cloud ID passed to the tools                |

## How it works

`JiraAPI` builds REST requests and passes them to a client's `make_request`. In MCP mode, `McpJiraClient`
(`src/jiratui/api/mcp_client.py`) replaces that client and maps each REST path to an MCP tool (for example
`POST search/jql` → `searchJiraIssuesUsingJql`). The MCP tools return Jira's REST JSON, so the rest of the app doesn't
change. `AtlassianMcpSession` (`src/jiratui/api/mcp_session.py`) keeps one MCP connection open for the app's lifetime
and handles OAuth.
