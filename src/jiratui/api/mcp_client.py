"""Drop-in replacements for the Jira REST clients that route every request through the Atlassian MCP server.

`JiraAPI` and `JiraSoftwareCloudAPI` build Jira REST requests (method + relative URL + params/payload) and hand them
to a client's `make_request`. `McpJiraClient.make_request` maps those requests onto the tools exposed by the Atlassian
Remote MCP server and returns responses shaped like the REST API ones, so the rest of the application is unaware of
the transport. The MCP server returns raw Jira REST payloads for most tools; where it does not (or where no tool exists)
the responses are synthesized here.

Operations the MCP server does not offer (deleting things, attachments, remote links, boards/sprints...) raise
`ServiceInvalidRequestException` with an explanatory message.
"""

from collections import defaultdict
import html
import json
import re
from typing import Any, Awaitable, Callable

import httpx

from jiratui.api.mcp_session import AtlassianMcpSession
from jiratui.exceptions import ResourceNotFoundException, ServiceInvalidRequestException

_HTTP_VERBS = {
    httpx.AsyncClient.get: 'GET',
    httpx.AsyncClient.post: 'POST',
    httpx.AsyncClient.put: 'PUT',
    httpx.AsyncClient.delete: 'DELETE',
    httpx.get: 'GET',
    httpx.post: 'POST',
    httpx.put: 'PUT',
    httpx.delete: 'DELETE',
}

# how many recently updated work items to sample when deriving data the MCP server has no tool for (statuses, users)
_SAMPLE_SIZE = 100

Handler = Callable[..., Awaitable[Any]]


class McpNotSupportedException(ServiceInvalidRequestException):
    pass


class McpJiraClient:
    """Async JSON client with the same interface as `AsyncJiraClient` but backed by the Atlassian MCP server."""

    def __init__(self, session: AtlassianMcpSession, cloud_id: str, base_url: str):
        self.session = session
        self.cloud_id = cloud_id
        self.base_url = base_url.rstrip('/')
        self._cache: dict[str, Any] = {}
        self._routes: list[tuple[str, re.Pattern, Handler]] = [
            (verb, re.compile(f'^{pattern}$'), handler)
            for verb, pattern, handler in [
                ('GET', r'project/search', self._search_projects),
                ('GET', r'project', self._all_projects),
                ('GET', r'project/(?P<key>[^/]+)/statuses', self._project_statuses),
                ('GET', r'project/(?P<key>[^/]+)', self._get_project),
                ('GET', r'issuetype', self._issue_types),
                ('GET', r'status', self._statuses),
                ('GET', r'statuses/search', self._statuses_search),
                ('GET', r'user/assignable/search', self._users_list),
                ('GET', r'user/assignable/multiProjectSearch', self._users_list),
                ('GET', r'users/search', self._users_list),
                ('GET', r'user/search', self._users_list),
                ('GET', r'user/picker', self._user_picker),
                ('GET', r'user', self._get_user),
                ('GET', r'myself', self._myself),
                ('GET', r'serverInfo', self._server_info),
                ('GET', r'configuration', self._global_settings),
                ('GET', r'filter/favourite', self._empty_list),
                ('GET', r'jql/autocompletedata/suggestions', self._empty_suggestions),
                ('GET', r'field', self._fields),
                ('GET', r'issue/picker', self._issue_picker),
                ('POST', r'search/jql', self._search_jql),
                ('POST', r'search', self._search_jql),
                ('POST', r'search/approximate-count', self._count),
                (
                    'GET',
                    r'issue/createmeta/(?P<project>[^/]+)/issuetypes/(?P<type_id>[^/]+)',
                    self._create_meta,
                ),
                ('POST', r'issue', self._create_issue),
                ('GET', r'issue/(?P<key>[^/]+)', self._get_issue),
                ('PUT', r'issue/(?P<key>[^/]+)', self._update_issue),
                ('GET', r'issue/(?P<key>[^/]+)/editmeta', self._edit_meta),
                ('GET', r'issue/(?P<key>[^/]+)/transitions', self._transitions),
                ('POST', r'issue/(?P<key>[^/]+)/transitions', self._transition),
                ('GET', r'issue/(?P<key>[^/]+)/comment', self._comments),
                ('POST', r'issue/(?P<key>[^/]+)/comment', self._add_comment),
                ('GET', r'issue/(?P<key>[^/]+)/comment/(?P<comment_id>[^/]+)', self._get_comment),
                ('PUT', r'issue/(?P<key>[^/]+)/comment/(?P<comment_id>[^/]+)', self._add_comment),
                ('GET', r'issue/(?P<key>[^/]+)/worklog', self._worklogs),
                ('POST', r'issue/(?P<key>[^/]+)/worklog', self._add_worklog),
                ('PUT', r'issue/(?P<key>[^/]+)/worklog/(?P<worklog_id>[^/]+)', self._add_worklog),
                ('GET', r'issue/(?P<key>[^/]+)/remotelink', self._remote_links),
                ('GET', r'issueLinkType', self._link_types),
                ('POST', r'issueLink', self._create_link),
                # Jira Software (agile) API: boards and sprints are not available through MCP
                ('GET', r'board', self._empty_page),
                ('GET', r'board/(?P<board_id>[^/]+)/sprint', self._empty_page),
            ]
        ]

    async def close_async_client(self) -> None:
        await self.session.close()

    async def make_request(
        self,
        method: Callable,
        url: str,
        headers: dict | None = None,
        timeout: int = 55,
        params: dict | None = None,
        data: str | None = None,
        json: dict | None = None,
        **kwargs,
    ) -> Any:
        verb = _HTTP_VERBS.get(method, getattr(method, '__name__', '').upper())
        path = url.split('?', 1)[0].strip('/')
        body: dict = json if json is not None else (_json_loads(data) if data else {})
        for route_verb, pattern, handler in self._routes:
            if route_verb == verb and (match := pattern.match(path)):
                return await handler(params=params or {}, body=body, **match.groupdict())
        raise McpNotSupportedException(
            f'"{verb} {path}" is not available through the Atlassian MCP server.',
            extra={'url': path, 'errorMessages': [f'{verb} {path} is not supported in MCP mode.']},
        )

    async def _tool(self, name: str, **arguments) -> Any:
        return await self.session.call_tool(name, {'cloudId': self.cloud_id, **arguments})

    async def _cached(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        if key not in self._cache:
            self._cache[key] = await factory()
        return self._cache[key]

    # Projects

    async def _search_projects(self, params: dict, body: dict) -> dict:
        if keys := params.get('keys'):
            wanted = [k.strip() for k in keys.split(',') if k.strip()]
            values = [project for key in wanted if (project := await self._find_project(key))]
            return {'values': values, 'total': len(values), 'startAt': 0, 'isLast': True}
        return await self._tool(
            'getVisibleJiraProjects',
            action='browse',
            searchString=params.get('query'),
            startAt=params.get('startAt', 0),
            maxResults=min(int(params.get('maxResults', 50)), 50),
            expandIssueTypes=False,
        )

    async def _all_projects(self, params: dict, body: dict) -> list[dict]:
        projects: list[dict] = []
        start_at = 0
        while True:
            page = await self._tool(
                'getVisibleJiraProjects', action='browse', startAt=start_at, maxResults=50
            )
            projects.extend(page.get('values', []))
            if page.get('isLast', True) or not page.get('values'):
                return projects
            start_at += len(page['values'])

    async def _find_project(self, key_or_id: str) -> dict | None:
        async def fetch() -> dict | None:
            page = await self._tool(
                'getVisibleJiraProjects', action='browse', searchString=key_or_id, maxResults=50
            )
            for project in page.get('values', []):
                if key_or_id in (project.get('key'), str(project.get('id'))):
                    return project
            return None

        return await self._cached(f'project:{key_or_id}', fetch)

    async def _get_project(self, params: dict, body: dict, key: str) -> dict:
        if project := await self._find_project(key):
            return project
        raise ResourceNotFoundException(f'Project {key} not found.', extra={'project': key})

    async def _issue_types(self, params: dict, body: dict) -> list[dict]:
        issue_types: dict[str, dict] = {}
        for project in await self._all_projects(params, body):
            for issue_type in project.get('issueTypes', []):
                issue_types.setdefault(str(issue_type.get('id')), issue_type)
        return list(issue_types.values())

    async def _sample_issues(self, jql: str, fields: list[str]) -> list[dict]:
        response = await self._tool(
            'searchJiraIssuesUsingJql', jql=jql, fields=fields, maxResults=_SAMPLE_SIZE
        )
        return response.get('issues', [])

    async def _project_statuses(self, params: dict, body: dict, key: str) -> list[dict]:
        """There is no MCP tool for a project's workflow statuses, so derive them from recent work items: every
        status observed per work item type, falling back to all observed statuses for types with no recent items."""

        async def fetch() -> list[dict]:
            project = await self._get_project(params, body, key)
            issues = await self._sample_issues(
                f'project = "{project["key"]}" ORDER BY updated DESC', ['status', 'issuetype']
            )
            by_type: dict[str, dict[str, dict]] = defaultdict(dict)
            all_statuses: dict[str, dict] = {}
            for issue in issues:
                fields = issue.get('fields', {})
                status = fields.get('status') or {}
                type_id = str((fields.get('issuetype') or {}).get('id'))
                by_type[type_id][str(status.get('id'))] = status
                all_statuses[str(status.get('id'))] = status
            return [
                {
                    'id': str(issue_type.get('id')),
                    'name': issue_type.get('name'),
                    'subtask': issue_type.get('subtask', False),
                    'statuses': list(
                        (by_type.get(str(issue_type.get('id'))) or all_statuses).values()
                    ),
                }
                for issue_type in project.get('issueTypes', [])
            ]

        return await self._cached(f'project-statuses:{key}', fetch)

    async def _statuses(self, params: dict, body: dict) -> list[dict]:
        async def fetch() -> list[dict]:
            issues = await self._sample_issues('updated >= -60d ORDER BY updated DESC', ['status'])
            statuses = {
                str(status['id']): status
                for issue in issues
                if (status := issue.get('fields', {}).get('status'))
            }
            return sorted(statuses.values(), key=lambda s: s.get('name', ''))

        return await self._cached('statuses', fetch)

    async def _statuses_search(self, params: dict, body: dict) -> dict:
        values = await self._statuses(params, body)
        return {'values': values, 'total': len(values), 'startAt': 0, 'isLast': True}

    # Users

    @staticmethod
    def _user_from_lookup(user: dict) -> dict:
        # the user picker returns "Name - email" with the matched text highlighted in HTML
        text = html.unescape(re.sub(r'<[^>]+>', '', user.get('html', '')))
        email = text.rsplit(' - ', 1)[1].strip() if ' - ' in text else ''
        return {
            'accountId': user.get('accountId'),
            'accountType': user.get('accountType'),
            'displayName': user.get('displayName'),
            'emailAddress': email or None,
            'active': True,
        }

    async def _lookup_users(self, query: str) -> list[dict]:
        response = await self._tool('lookupJiraAccountId', searchString=query)
        users = response.get('data', {}).get('users', {}).get('users', [])
        return [self._user_from_lookup(user) for user in users]

    async def _recent_users(self, project_keys: str | None) -> list[dict]:
        """The user picker requires a search string; to list users without one, collect the people assigned to or
        reporting recently updated work items."""

        async def fetch() -> list[dict]:
            projects = ', '.join(f'"{key}"' for key in (project_keys or '').split(',') if key)
            jql = f'project in ({projects}) ' if projects else 'updated >= -30d '
            issues = await self._sample_issues(
                jql + 'ORDER BY updated DESC', ['assignee', 'reporter']
            )
            users: dict[str, dict] = {}
            for issue in issues:
                for role in ('assignee', 'reporter'):
                    if (user := issue.get('fields', {}).get(role)) and user.get('accountId'):
                        users.setdefault(user['accountId'], user)
            me = await self._myself({}, {})
            users.setdefault(me['accountId'], me)
            return sorted(users.values(), key=lambda u: u.get('displayName', ''))

        return await self._cached(f'users:{project_keys}', fetch)

    async def _users_list(self, params: dict, body: dict) -> list[dict]:
        query = params.get('query') or params.get('username') or ''
        if query:
            return await self._lookup_users(query)
        project = params.get('project') or params.get('projectKeys')
        if not project and (issue_key := params.get('issueKey')):
            project = issue_key.rsplit('-', 1)[0]
        users = await self._recent_users(project)
        start = int(params.get('startAt', 0))
        return users[start : start + int(params.get('maxResults', 1000))]

    async def _user_picker(self, params: dict, body: dict) -> dict:
        users = await self._lookup_users(params.get('query', ''))
        return {
            'users': [
                {**user, 'html': f'{user["displayName"]} - {user.get("emailAddress") or ""}'}
                for user in users
            ],
            'total': len(users),
        }

    async def _get_user(self, params: dict, body: dict) -> dict:
        account_id = params.get('accountId', '')
        for user in await self._lookup_users(account_id):
            if user['accountId'] == account_id:
                return user
        raise ResourceNotFoundException(
            f'User {account_id} not found.', extra={'accountId': account_id}
        )

    async def _myself(self, params: dict, body: dict) -> dict:
        async def fetch() -> dict:
            info = await self.session.call_tool('atlassianUserInfo', {})
            return {
                'accountId': info.get('account_id'),
                'accountType': info.get('account_type', 'atlassian'),
                'displayName': info.get('name'),
                'emailAddress': info.get('email'),
                'active': info.get('account_status', 'active') == 'active',
                'timeZone': info.get('zoneinfo'),
                'locale': info.get('locale'),
            }

        return await self._cached('myself', fetch)

    # Server

    async def _server_info(self, params: dict, body: dict) -> dict:
        return {
            'baseUrl': self.base_url,
            'displayUrl': self.base_url,
            'deploymentType': 'Cloud',
            'serverTitle': 'Jira (via Atlassian MCP)',
            'version': 'MCP',
        }

    async def _global_settings(self, params: dict, body: dict) -> dict:
        return {
            'attachmentsEnabled': False,
            'issueLinkingEnabled': True,
            'subTasksEnabled': True,
            'unassignedIssuesAllowed': True,
            'votingEnabled': False,
            'watchingEnabled': False,
            'timeTrackingEnabled': True,
        }

    async def _empty_list(self, params: dict, body: dict) -> list:
        return []

    async def _empty_page(self, params: dict, body: dict, **kwargs) -> dict:
        return {'values': [], 'total': 0, 'startAt': 0, 'isLast': True}

    async def _empty_suggestions(self, params: dict, body: dict) -> dict:
        return {'results': []}

    async def _fields(self, params: dict, body: dict) -> list[dict]:
        """There is no MCP tool to list fields; read the field names and schemas of a recently updated work item."""

        async def fetch() -> list[dict]:
            issues = await self._sample_issues('updated >= -30d ORDER BY updated DESC', ['summary'])
            if not issues:
                return []
            issue = await self._tool(
                'getJiraIssue',
                issueIdOrKey=issues[0]['key'],
                fields=['*all'],
                expand='names,schema',
            )
            schemas: dict = issue.get('schema', {})
            return [
                {
                    'id': field_id,
                    'key': field_id,
                    'name': name,
                    'custom': field_id.startswith('customfield_'),
                    'schema': schemas.get(field_id, {}),
                }
                for field_id, name in issue.get('names', {}).items()
            ]

        return await self._cached('fields', fetch)

    # Work items

    async def _search_jql(self, params: dict, body: dict) -> dict:
        response = await self._tool(
            'searchJiraIssuesUsingJql',
            jql=body.get('jql', ''),
            fields=body.get('fields') or None,
            maxResults=max(1, min(int(body.get('maxResults', 50)), 100)),
            nextPageToken=body.get('nextPageToken'),
            responseContentFormat='adf',
        )
        response.setdefault('issues', [])
        return response

    async def _count(self, params: dict, body: dict) -> dict:
        response = await self._tool(
            'searchJiraIssuesUsingJql', jql=body.get('jql', ''), searchResultMode='count'
        )
        return {'count': response.get('totalCount', 0)}

    async def _issue_picker(self, params: dict, body: dict) -> dict:
        query = (params.get('query') or '').strip()
        text = query.replace('\\', '\\\\').replace('"', '\\"')
        clauses = [f'text ~ "{text}*"'] if text else []
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d+', query):
            clauses.insert(0, f'key = "{query.upper()}"')
        jql = f'({" OR ".join(clauses)})' if clauses else 'updated >= -30d'
        if project_id := params.get('currentProjectId'):
            jql += f' AND project = {project_id}'
        if current := params.get('currentIssueKey'):
            jql += f' AND key != "{current}"'
        if str(params.get('showSubTasks', True)).lower() == 'false':
            jql += ' AND issuetype not in subTaskIssueTypes()'
        response = await self._tool(
            'searchJiraIssuesUsingJql',
            jql=f'{jql} ORDER BY updated DESC',
            fields=['summary'],
            maxResults=50,
        )
        issues = [
            {
                'id': issue['id'],
                'key': issue['key'],
                'summary': issue['fields'].get('summary', ''),
                'summaryText': issue['fields'].get('summary', ''),
            }
            for issue in response.get('issues', [])
        ]
        return {'sections': [{'id': 'cs', 'label': 'Current Search', 'issues': issues}]}

    async def _get_issue(self, params: dict, body: dict, key: str) -> dict:
        fields = params.get('fields')
        return await self._tool(
            'getJiraIssue',
            issueIdOrKey=key,
            # the REST API returns all fields by default; the MCP tool only a handful
            fields=fields.split(',') if fields else ['*all'],
            expand=params.get('expand'),
            responseContentFormat='adf',
        )

    async def _edit_meta(self, params: dict, body: dict, key: str) -> dict:
        issue = await self._tool(
            'getJiraIssue', issueIdOrKey=key, fields=['*all'], expand='editmeta'
        )
        return issue.get('editmeta', {'fields': {}})

    @staticmethod
    def _fields_from_update(body: dict) -> dict:
        """The MCP tools only accept `fields`; fold `update` "set" operations into them."""
        fields = dict(body.get('fields') or {})
        for field_id, operations in (body.get('update') or {}).items():
            for operation in operations:
                if 'set' not in operation:
                    raise McpNotSupportedException(
                        f'The "{next(iter(operation))}" operation on {field_id} is not supported in MCP mode.'
                    )
                fields[field_id] = operation['set']
        return fields

    async def _update_issue(self, params: dict, body: dict, key: str) -> dict:
        return await self._tool(
            'editJiraIssue',
            issueIdOrKey=key,
            fields=self._fields_from_update(body),
            contentFormat='adf',
            responseContentFormat='adf',
        )

    async def _issue_type_name(self, project_key: str, issue_type: dict) -> str:
        if name := issue_type.get('name'):
            return name
        project = await self._get_project({}, {}, project_key)
        for candidate in project.get('issueTypes') or []:
            if str(candidate.get('id')) == str(issue_type.get('id')):
                return candidate['name']
        response = await self._tool(
            'getJiraProjectIssueTypesMetadata', projectIdOrKey=project_key, maxResults=200
        )
        for candidate in response.get('issueTypes', []):
            if str(candidate.get('id')) == str(issue_type.get('id')):
                return candidate['name']
        raise ResourceNotFoundException(f'Unknown work item type {issue_type}.')

    async def _create_issue(self, params: dict, body: dict) -> dict:
        fields = self._fields_from_update(body)
        project = fields.pop('project', {})
        project_key = (
            project.get('key') or (await self._get_project({}, {}, str(project.get('id'))))['key']
        )
        issue_type_name = await self._issue_type_name(project_key, fields.pop('issuetype', {}))
        assignee = fields.pop('assignee', None) or {}
        parent = fields.pop('parent', None) or {}
        response = await self._tool(
            'createJiraIssue',
            projectKey=project_key,
            issueTypeName=issue_type_name,
            summary=fields.pop('summary', ''),
            description=fields.pop('description', None),
            assignee_account_id=assignee.get('accountId'),
            parent=parent.get('key') or parent.get('id'),
            additional_fields=fields or None,
            contentFormat='adf',
        )
        return {'id': response.get('id'), 'key': response.get('key'), 'self': response.get('self')}

    async def _create_meta(self, params: dict, body: dict, project: str, type_id: str) -> dict:
        response = await self._tool(
            'getJiraIssueTypeMetaWithFields',
            projectIdOrKey=project,
            issueTypeId=type_id,
            requiredFieldsOnly=False,
            startAt=params.get('startAt'),
            maxResults=params.get('maxResults'),
        )
        # the REST API calls the list "fields"; tolerate either name
        if 'fields' not in response and 'values' in response:
            response['fields'] = response['values']
        return response

    async def _transitions(self, params: dict, body: dict, key: str) -> dict:
        return await self._tool('getTransitionsForJiraIssue', issueIdOrKey=key)

    async def _transition(self, params: dict, body: dict, key: str) -> dict:
        await self._tool(
            'transitionJiraIssue',
            issueIdOrKey=key,
            transition=body['transition'],
            fields=body.get('fields'),
            update=body.get('update'),
        )
        return {}

    # Comments

    async def _comments(self, params: dict, body: dict, key: str) -> dict:
        issue = await self._tool(
            'getJiraIssue', issueIdOrKey=key, fields=['comment'], responseContentFormat='adf'
        )
        comments = issue.get('fields', {}).get('comment', {}) or {}
        all_comments = comments.get('comments', [])
        if params.get('orderBy') == '-created':
            all_comments = list(reversed(all_comments))
        start = int(params.get('startAt', 0))
        limit = int(params.get('maxResults', len(all_comments) or 1))
        page = all_comments[start : start + limit]
        return {
            'comments': page,
            'startAt': start,
            'maxResults': limit,
            'total': comments.get('total', len(all_comments)),
        }

    async def _get_comment(self, params: dict, body: dict, key: str, comment_id: str) -> dict:
        for comment in (await self._comments({'maxResults': 5000}, {}, key))['comments']:
            if str(comment.get('id')) == comment_id:
                return comment
        raise ResourceNotFoundException(f'Comment {comment_id} not found.', extra={'issue': key})

    async def _add_comment(
        self, params: dict, body: dict, key: str, comment_id: str | None = None
    ) -> dict:
        comment_body = body.get('body', '')
        is_adf = isinstance(comment_body, dict)
        return await self._tool(
            'addCommentToJiraIssue',
            issueIdOrKey=key,
            commentId=comment_id,
            commentBody=json.dumps(comment_body) if is_adf else comment_body,
            contentFormat='adf' if is_adf else 'markdown',
            responseContentFormat='adf',
        )

    # Work logs

    async def _worklogs(self, params: dict, body: dict, key: str) -> dict:
        issue = await self._tool('getJiraIssue', issueIdOrKey=key, fields=['worklog'])
        worklog = issue.get('fields', {}).get('worklog', {}) or {}
        worklog.setdefault('worklogs', [])
        return worklog

    async def _add_worklog(
        self, params: dict, body: dict, key: str, worklog_id: str | None = None
    ) -> dict:
        if params.get('adjustEstimate') == 'new':
            raise McpNotSupportedException(
                'Setting the remaining estimate is not supported in MCP mode.'
            )
        comment = body.get('comment')
        time_spent = body.get('timeSpent')
        if not time_spent and body.get('timeSpentSeconds'):
            time_spent = f'{int(body["timeSpentSeconds"]) // 60}m'
        return await self._tool(
            'addWorklogToJiraIssue',
            issueIdOrKey=key,
            worklogId=worklog_id,
            timeSpent=time_spent,
            started=body.get('started'),
            commentBody=json.dumps(comment) if isinstance(comment, dict) else comment,
            contentFormat='adf' if isinstance(comment, dict) else None,
        )

    # Links

    async def _remote_links(self, params: dict, body: dict, key: str) -> list[dict]:
        response = await self._tool('getJiraIssueRemoteIssueLinks', issueIdOrKey=key)
        return response if isinstance(response, list) else response.get('values', [])

    async def _link_types(self, params: dict, body: dict) -> dict:
        return await self._cached('link-types', lambda: self._tool('getIssueLinkTypes'))

    async def _create_link(self, params: dict, body: dict) -> dict:
        link_type = body.get('type', {})
        name = link_type.get('name')
        if not name:
            for candidate in (await self._link_types({}, {})).get('issueLinkTypes', []):
                if str(candidate.get('id')) == str(link_type.get('id')):
                    name = candidate['name']
        await self._tool(
            'createIssueLink',
            inwardIssue=body['inwardIssue']['key'],
            outwardIssue=body['outwardIssue']['key'],
            type=name,
        )
        return {}


class McpUnsupportedClient:
    """Stands in for the clients used for attachments, which the MCP server can not upload or download."""

    def __init__(self, base_url: str = ''):
        self.base_url = base_url

    def _unsupported(self, *args, **kwargs):
        raise McpNotSupportedException(
            'Attachments are not available through the Atlassian MCP server.'
        )

    make_request = _unsupported

    async def close_async_client(self) -> None:
        return None


def _json_loads(data: str | bytes) -> dict:
    try:
        return json.loads(data)
    except (TypeError, ValueError):
        return {}
