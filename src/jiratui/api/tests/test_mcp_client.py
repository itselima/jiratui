from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest

from jiratui.api.api import JiraMcpAPI
from jiratui.api.mcp_client import McpNotSupportedException
from jiratui.config import ApplicationConfiguration
from jiratui.exceptions import ResourceNotFoundException


class FakeMcpSession:
    """Records tool calls and answers them with canned responses."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        arguments = {k: v for k, v in arguments.items() if v is not None}
        self.calls.append((name, arguments))
        response = self.responses[name]
        return response(arguments) if callable(response) else response

    async def close(self):
        pass


@pytest.fixture()
def mcp_config() -> ApplicationConfiguration:
    config_mock = Mock(spec=ApplicationConfiguration)
    config_mock.configure_mock(
        use_mcp=True,
        jira_api_base_url='https://acme.atlassian.net',
        mcp_cloud_id=None,
        enable_logging=False,
    )
    return config_mock


def make_api(
    config: ApplicationConfiguration, responses: dict
) -> tuple[JiraMcpAPI, FakeMcpSession]:
    session = FakeMcpSession(responses)
    return JiraMcpAPI(session, config), session  # type:ignore[arg-type]


PROJECT = {
    'id': '10077',
    'key': 'ELUME',
    'name': 'ELUME',
    'issueTypes': [{'id': '10007', 'name': 'Task'}, {'id': '10009', 'name': 'Bug'}],
}


@pytest.mark.asyncio
async def test_search_issues_uses_jql_tool_with_cloud_id_from_base_url(mcp_config):
    api, session = make_api(
        mcp_config, {'searchJiraIssuesUsingJql': {'issues': [{'key': 'ELUME-1'}], 'isLast': True}}
    )

    result = await api.search_issues(
        project_key='ELUME', jql_query='assignee = currentUser()', fields=['summary'], limit=30
    )

    assert result['issues'] == [{'key': 'ELUME-1'}]
    name, arguments = session.calls[0]
    assert name == 'searchJiraIssuesUsingJql'
    assert arguments['cloudId'] == 'acme.atlassian.net'
    assert 'project = "ELUME"' in arguments['jql'] or 'project = ELUME' in arguments['jql']
    assert arguments['fields'] == ['summary']
    assert arguments['maxResults'] == 30
    assert arguments['responseContentFormat'] == 'adf'


@pytest.mark.asyncio
async def test_get_issue_requests_all_fields_by_default(mcp_config):
    api, session = make_api(mcp_config, {'getJiraIssue': {'key': 'ELUME-1', 'fields': {}}})

    await api.get_issue('ELUME-1')

    assert session.calls[0][1]['fields'] == ['*all']
    assert session.calls[0][1]['expand'] == 'editmeta'


@pytest.mark.asyncio
async def test_update_issue_folds_set_operations_into_fields(mcp_config):
    api, session = make_api(mcp_config, {'editJiraIssue': {'key': 'ELUME-1'}})

    await api.update_issue(
        'ELUME-1',
        {'fields': {'summary': 'New'}, 'update': {'labels': [{'set': ['a', 'b']}]}},
    )

    name, arguments = session.calls[0]
    assert name == 'editJiraIssue'
    assert arguments['fields'] == {'summary': 'New', 'labels': ['a', 'b']}
    assert arguments['contentFormat'] == 'adf'


@pytest.mark.asyncio
async def test_create_work_item_resolves_project_and_issue_type_names(mcp_config):
    api, session = make_api(
        mcp_config,
        {
            'getVisibleJiraProjects': {'values': [PROJECT]},
            'createJiraIssue': {'id': '1', 'key': 'ELUME-9', 'self': 'x'},
        },
    )
    description = {'type': 'doc', 'version': 1, 'content': []}

    result = await api.create_work_item(
        {
            'project': {'id': '10077'},
            'issuetype': {'id': '10009'},
            'summary': 'Broken',
            'description': description,
            'assignee': {'accountId': 'abc'},
            'priority': {'id': '3'},
        }
    )

    assert result == {'id': '1', 'key': 'ELUME-9', 'self': 'x'}
    arguments = session.calls[-1][1]
    assert arguments['projectKey'] == 'ELUME'
    assert arguments['issueTypeName'] == 'Bug'
    assert arguments['description'] == description
    assert arguments['assignee_account_id'] == 'abc'
    assert arguments['additional_fields'] == {'priority': {'id': '3'}}


@pytest.mark.asyncio
async def test_project_statuses_are_derived_from_recent_work_items(mcp_config):
    api, _ = make_api(
        mcp_config,
        {
            'getVisibleJiraProjects': {'values': [PROJECT]},
            'searchJiraIssuesUsingJql': {
                'issues': [
                    {
                        'fields': {
                            'status': {'id': '1', 'name': 'To Do'},
                            'issuetype': {'id': '10007'},
                        }
                    },
                    {
                        'fields': {
                            'status': {'id': '2', 'name': 'Done'},
                            'issuetype': {'id': '10007'},
                        }
                    },
                ]
            },
        },
    )

    result = await api.get_project_statuses('ELUME')

    by_type = {record['name']: [s['name'] for s in record['statuses']] for record in result}
    assert by_type['Task'] == ['To Do', 'Done']
    # no recent bugs: fall back to every status seen in the project
    assert by_type['Bug'] == ['To Do', 'Done']


@pytest.mark.asyncio
async def test_users_with_query_use_lookup_and_parse_email(mcp_config):
    api, _ = make_api(
        mcp_config,
        {
            'lookupJiraAccountId': {
                'data': {
                    'users': {
                        'users': [
                            {
                                'accountId': 'abc',
                                'displayName': 'Amy White',
                                'html': '<strong>A</strong>my White - <strong>a</strong>my@acme.com',
                            }
                        ]
                    }
                }
            }
        },
    )

    result = await api.user_assignable_search(project_id_or_key='ELUME', query='a')

    assert result == [
        {
            'accountId': 'abc',
            'accountType': None,
            'displayName': 'Amy White',
            'emailAddress': 'amy@acme.com',
            'active': True,
        }
    ]


@pytest.mark.asyncio
async def test_comments_are_read_from_the_issue(mcp_config):
    comments = [{'id': '1'}, {'id': '2'}, {'id': '3'}]
    api, _ = make_api(
        mcp_config,
        {'getJiraIssue': {'fields': {'comment': {'comments': comments, 'total': 3}}}},
    )

    page = await api.get_comments('ELUME-1', offset=1, limit=1)
    comment = await api.get_comment('ELUME-1', '3')

    assert page['comments'] == [{'id': '2'}]
    assert page['total'] == 3
    assert comment == {'id': '3'}
    with pytest.raises(ResourceNotFoundException):
        await api.get_comment('ELUME-1', '99')


@pytest.mark.asyncio
async def test_add_adf_comment(mcp_config):
    api, session = make_api(mcp_config, {'addCommentToJiraIssue': {'id': '5'}})
    body = {'type': 'doc', 'version': 1, 'content': []}

    await api.add_comment('ELUME-1', body)

    arguments = session.calls[0][1]
    assert json.loads(arguments['commentBody']) == body
    assert arguments['contentFormat'] == 'adf'


@pytest.mark.asyncio
async def test_add_worklog(mcp_config):
    api, session = make_api(mcp_config, {'addWorklogToJiraIssue': {'id': '7'}})

    await api.add_issue_work_log(
        'ELUME-1', started=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc), time_spent='2h'
    )

    arguments = session.calls[0][1]
    assert arguments['timeSpent'] == '2h'
    assert arguments['started'] == '2026-10-01T09:00:00.000+0000'


@pytest.mark.asyncio
async def test_create_issue_link_resolves_link_type_name(mcp_config):
    api, session = make_api(
        mcp_config,
        {
            'getIssueLinkTypes': {'issueLinkTypes': [{'id': '10000', 'name': 'Blocks'}]},
            'createIssueLink': {},
        },
    )

    await api.create_issue_link('ELUME-1', 'ELUME-2', link_type='outward', link_type_id='10000')

    assert session.calls[-1] == (
        'createIssueLink',
        {
            'cloudId': 'acme.atlassian.net',
            'inwardIssue': 'ELUME-1',
            'outwardIssue': 'ELUME-2',
            'type': 'Blocks',
        },
    )


@pytest.mark.asyncio
async def test_myself_maps_user_info(mcp_config):
    api, _ = make_api(
        mcp_config,
        {'atlassianUserInfo': {'account_id': 'abc', 'name': 'Edi', 'email': 'e@acme.com'}},
    )

    result = await api.myself()

    assert result['accountId'] == 'abc'
    assert result['displayName'] == 'Edi'
    assert result['emailAddress'] == 'e@acme.com'


@pytest.mark.asyncio
async def test_unsupported_operations_raise(mcp_config):
    api, _ = make_api(mcp_config, {})

    with pytest.raises(McpNotSupportedException):
        await api.delete_work_item('ELUME-1')
    with pytest.raises(McpNotSupportedException):
        await api.get_attachment_content('1')
