import copy
import datetime as dt
import io
import os
import re
from typing import List
import urllib.parse
import pathlib

import dateutil.parser
import pytest
import responses

from harmony.harmony import BBox, Client, Collection, LinkType, ProcessingFailedException, Request, Dimension


@pytest.fixture()
def examples_dir():
    return pathlib.Path(__file__).parent.parent.joinpath('examples').absolute()


def expected_submit_url(collection_id, variables='all'):
    return (f'https://harmony.earthdata.nasa.gov/{collection_id}'
            f'/ogc-api-coverages/1.0.0/collections/{variables}/coverage/rangeset')


def expected_status_url(job_id, link_type: LinkType = LinkType.https):
    return f'https://harmony.earthdata.nasa.gov/jobs/{job_id}?linktype={link_type.value}'

def expected_pause_url(job_id, link_type: LinkType = LinkType.https):
    return f'https://harmony.earthdata.nasa.gov/jobs/{job_id}/pause?linktype={link_type.value}'

def expected_resume_url(job_id, link_type: LinkType = LinkType.https):
    return f'https://harmony.earthdata.nasa.gov/jobs/{job_id}/resume?linktype={link_type.value}'

def expected_full_submit_url(request):
    async_params = ['forceAsync=true']

    spatial_params = []
    if request.spatial:
        w, s, e, n = request.spatial
        spatial_params = [f'subset=lat({s}:{n})', f'subset=lon({w}:{e})']

    temporal_params = []
    if request.temporal:
        start = request.temporal['start']
        stop = request.temporal['stop']
        temporal_params = [f'subset=time("{start.isoformat()}":"{stop.isoformat()}")']

    dimension_params = []
    if request.dimensions:
        for dim in request.dimensions:
            name = dim.name
            min = dim.min if dim.min is not None else '*'
            max = dim.max if dim.max is not None else '*'
            dimension_params += [f'subset={name}({min}:{max})']

    query_params = '&'.join(async_params + spatial_params + temporal_params + dimension_params)
    if request.format is not None:
        query_params += f'&format{request.format}'
    if request.skip_preview is not None:
        query_params += f'&skipPreview={str(request.skip_preview).lower()}'

    return f'{expected_submit_url(request.collection.id)}?{query_params}'


def fake_data_url(link_type: LinkType = LinkType.https):
    if link_type == LinkType.s3:
        fake_data_url = f'{link_type.value}://fakebucket/public/harmony/foo'
    else:
        fake_data_url = f'{link_type.value}://harmony.earthdata.nasa.gov/service-results'
    return f'{fake_data_url}/fake.tif'


def expected_user_agent_header_regex():
    # Since it's kinda overkill to find the exact character set
    #   allowed in platform/implementation/version/etc,
    #   the following regex may be a little bit more tolerant
    return r"\s*([^/\s]+/[^/\s]+)(\s+[^/\s]+/[^/\s]+)*\s*"


def expected_job(collection_id, job_id, link_type: LinkType = LinkType.https, extra_links=[]):
    return {
        'username': 'rfeynman',
        'status': 'running',
        'message': 'The job is being processed',
        'progress': 0,
        'createdAt': '2021-02-19T18:47:31.291Z',
        'updatedAt': '2021-02-19T18:47:31.291Z',
        'dataExpiration': '2021-03-21T18:47:31.291Z',
        'links': [
            {
                'title': 'Job Status',
                'href': f'https://harmony.earthdata.nasa.gov/jobs/{job_id}',
                'rel': 'self',
                'type': 'application/json'
            },
            {
                'title': 'STAC catalog',
                'href': f'https://harmony.earthdata.nasa.gov/stac/{job_id}/',
                'rel': 'stac-catalog-json',
                'type': 'application/json'
            },
            {
                'href': fake_data_url(link_type),
                'title': '2020_01_15_fake.nc.tif',
                'type': 'image/tiff',
                'rel': 'data',
                'bbox': [
                    -179.95,
                    -89.95,
                    179.95,
                    89.95
                ],
                'temporal': {
                    'start': '2020-01-15T00:00:00.000Z',
                    'end': '2020-01-15T23:59:59.000Z'
                }
            },
            *extra_links
        ],
        'request': (
            'https://harmony.earthdata.nasa.gov/{collection_id}/ogc-api-coverages/1.0.0'
            '/collections/all/coverage/rangeset'
            '?forceAsync=True'
            '&subset=lat(52%3A77)'
            '&subset=lon(-165%3A-140)'
            '&subset=time(%222010-01-01T00%3A00%3A00%22%3A%222020-12-30T00%3A00%3A00%22)'
        ),
        'numInputGranules': 32,
        'jobID': f'{job_id}'
    }

def expected_paused_job(collection_id, job_id, link_type: LinkType = LinkType.https, extra_links=[]):
    job = expected_job(collection_id, job_id, link_type, extra_links)
    job['status'] = 'paused'
    job['progress'] = 10
    return job


@responses.activate
def test_when_multiple_submits_it_only_authenticates_once():
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        spatial=BBox(-107, 40, -105, 42)
    )
    job_id = '3141592653-abcd-1234'
    auth_url = 'https://harmony.earthdata.nasa.gov/jobs'
    responses.add(
        responses.GET,
        auth_url,
        status=200
    )
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    client = Client()
    client.submit(request)
    client.submit(request)

    assert len(responses.calls) == 3
    assert responses.calls[0].request.url == auth_url
    assert urllib.parse.unquote(responses.calls[0].request.url) == auth_url
    assert urllib.parse.unquote(
        responses.calls[1].request.url) == expected_full_submit_url(request)
    assert urllib.parse.unquote(
        responses.calls[2].request.url) == expected_full_submit_url(request)


@responses.activate
def test_with_bounding_box():
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        spatial=BBox(-107, 40, -105, 42)
    )
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(
        responses.calls[0].request.url) == expected_full_submit_url(request)
    assert actual_job_id == job_id

@responses.activate
def test_with_single_dimension():
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        dimensions=[Dimension('foo', 0, 20.5)]
    )
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(
        responses.calls[0].request.url) == expected_full_submit_url(request)
    assert actual_job_id == job_id


@responses.activate
def test_with_multiple_dimensions():
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        dimensions=[
            Dimension('foo', None, 20.5),
            Dimension(name='bar', max=20.1, min=-10.2),
            Dimension('baz'),
            Dimension('alpha', -10),
            Dimension('bravo', max=30),
            Dimension('charlie', min=20),
            Dimension('delta', 0, 20.5),
        ]
    )
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(
        responses.calls[0].request.url) == expected_full_submit_url(request)
    assert actual_job_id == job_id

@responses.activate
def test_with_temporal_range():
    collection = Collection(id='C1234-TATOOINE')
    request = Request(
        collection=collection,
        temporal={
            'start': dt.datetime(2010, 12, 1),
            'stop': dt.datetime(2010, 12, 31)
        },
    )
    job_id = '1234abcd-deed-9876-c001-f00dbad'
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(
        responses.calls[0].request.url) == expected_full_submit_url(request)
    assert actual_job_id == job_id


@responses.activate
def test_with_bounding_box_and_temporal_range():
    collection = Collection(id='C333666999-EOSDIS')
    request = Request(
        collection=collection,
        spatial=BBox(-107, 40, -105, 42),
        temporal={
            'start': dt.datetime(2001, 1, 1),
            'stop': dt.datetime(2003, 3, 31)
        },
    )
    job_id = '1234abcd-1234-9876-6666-999999abcd'
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(
        responses.calls[0].request.url) == expected_full_submit_url(request)
    assert actual_job_id == job_id


@responses.activate
def test_with_shapefile(examples_dir):
    collection = Collection(id='C333666999-EOSDIS')
    request = Request(
        collection=collection,
        shape=os.path.join(examples_dir, 'asf_example.json'),
        spatial=BBox(-107, 40, -105, 42),
    )
    job_id = '1234abcd-1234-9876-6666-999999abcd'
    responses.add(
        responses.POST,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, job_id)
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1

    post_request = responses.calls[0].request
    post_body = post_request.body.decode('utf-8')

    assert post_request is not None

    # GeoJSON is present in the submit body
    assert 'FeatureCollection' in post_body
    assert 'Content-Type: application/geo+json' in post_body

    # Submit URL has no query params
    assert urllib.parse.unquote(post_request.url) == expected_submit_url(collection.id)

    # Would-be query params are in the POST body
    assert 'Content-Disposition: form-data; name="forceAsync"\r\n\r\ntrue' in post_body
    assert 'Content-Disposition: form-data; name="subset"\r\n\r\nlat(40:42)' in post_body
    assert 'Content-Disposition: form-data; name="subset"\r\n\r\nlon(-107:-105)' in post_body

    assert actual_job_id == job_id


def test_with_invalid_request():
    collection = Collection(id='C333666999-EOSDIS')
    request = Request(
        collection=collection,
        spatial=BBox(-190, -100, 100, 190)
    )

    with pytest.raises(Exception):
        Client(should_validate_auth=False).submit(request)


@responses.activate
def test_get_request_has_user_agent_headers():
    collection = Collection('foobar')
    request = Request(
        collection=collection,
    )
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, 'abcd-1234'),
    )

    Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert responses.calls[0].request.headers is not None
    headers = responses.calls[0].request.headers
    assert "User-Agent" in headers
    user_agent_header = headers["User-Agent"]
    assert re.match(
        expected_user_agent_header_regex(), user_agent_header
    )


@responses.activate
def test_post_request_has_user_agent_headers(examples_dir):
    collection = Collection('foobar')
    request = Request(
        collection=collection,
        shape=os.path.join(examples_dir, 'asf_example.json'),
        spatial=BBox(-107, 40, -105, 42),
    )
    job_id = '1234abcd-1234-9876-6666-999999abcd'
    responses.add(
        responses.POST,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, 'abcd-1234'),
    )

    actual_job_id = Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert responses.calls[0].request.headers is not None
    headers = responses.calls[0].request.headers
    assert "User-Agent" in headers
    user_agent_header = headers["User-Agent"]
    assert re.match(
        expected_user_agent_header_regex(), user_agent_header
    )


@pytest.mark.parametrize('param,expected', [
    ({'crs': 'epsg:3141'}, 'outputcrs=epsg:3141'),
    ({'format': 'r2d2/hologram'}, 'format=r2d2/hologram'),
    ({'granule_id': ['G1', 'G2', 'G3']}, 'granuleId=G1&granuleId=G2&granuleId=G3'),
    ({'height': 200}, 'height=200'),
    ({'interpolation': 'nearest'}, 'interpolation=nearest'),
    ({'max_results': 7}, 'maxResults=7'),
    ({'scale_extent': [1.0, 2.0, 1.0, 4.0]}, 'scaleExtent=1.0,2.0,1.0,4.0'),
    ({'scale_size': [1.0, 2.0]}, 'scaleSize=1.0,2.0'),
    ({'width': 100}, 'width=100'),
    ({'concatenate': True}, 'concatenate=true'),
])
@responses.activate
def test_request_has_query_param(param, expected):
    collection = Collection('foobar')
    request = Request(
        collection=collection,
        **param
    )
    responses.add(
        responses.GET,
        expected_submit_url(collection.id),
        status=200,
        json=expected_job(collection.id, 'abcd-1234'),
    )

    Client(should_validate_auth=False).submit(request)

    assert len(responses.calls) == 1
    assert urllib.parse.unquote(responses.calls[0].request.url).index(expected) >= 0


@responses.activate
@pytest.mark.parametrize('variables,expected', [
    (['one'], 'one'),
    (['red_var', 'green_var', 'blue_var'], 'red_var,green_var,blue_var'),
    (['/var/with/a/path'], '%2Fvar%2Fwith%2Fa%2Fpath')
])
def test_request_has_variables(variables, expected):
    collection = Collection('foobar')
    request = Request(
        collection=collection,
        variables=variables
    )
    responses.add(
        responses.GET,
        expected_submit_url(collection.id, expected),
        status=200,
        json=expected_job(collection.id, 'abcd-1234'),
    )

    Client(should_validate_auth=False).submit(request)


@responses.activate
def test_status():
    collection = Collection(id='C333666999-EOSDIS')
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_job = expected_job(collection.id, job_id)
    expected_status = {
        'status': exp_job['status'],
        'message': exp_job['message'],
        'progress': exp_job['progress'],
        'created_at': dateutil.parser.parse(exp_job['createdAt']),
        'updated_at': dateutil.parser.parse(exp_job['updatedAt']),
        'data_expiration': dateutil.parser.parse(exp_job['dataExpiration']),
        'created_at_local': dateutil.parser.parse(exp_job['createdAt']).replace(microsecond=0).astimezone().isoformat(),
        'updated_at_local': dateutil.parser.parse(exp_job['updatedAt']).replace(microsecond=0).astimezone().isoformat(),
        'data_expiration_local': dateutil.parser.parse(exp_job['dataExpiration']).replace(microsecond=0).astimezone().isoformat(),
        'request': exp_job['request'],
        'num_input_granules': exp_job['numInputGranules']}
    responses.add(
        responses.GET,
        expected_status_url(job_id),
        status=200,
        json=exp_job
    )

    actual_status = Client(should_validate_auth=False).status(job_id)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(responses.calls[0].request.url) == expected_status_url(job_id)
    assert actual_status == expected_status


@responses.activate
def test_progress():
    collection = Collection(id='C333666999-EOSDIS')
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_job = expected_job(collection.id, job_id)
    expected_progress = int(exp_job['progress']), exp_job['status'], exp_job['message']
    responses.add(
        responses.GET,
        expected_status_url(job_id),
        status=200,
        json=exp_job
    )

    actual_progress = Client(should_validate_auth=False).progress(job_id)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(responses.calls[0].request.url) == expected_status_url(job_id)
    assert actual_progress == expected_progress

@responses.activate
def test_pause():
    collection = Collection(id='C333666999-EOSDIS')
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_job = expected_paused_job(collection, job_id)
    responses.add(
        responses.GET,
        expected_pause_url(job_id),
        status=200,
        json=exp_job
    )

    Client(should_validate_auth=False).pause(job_id)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(responses.calls[0].request.url) == expected_pause_url(job_id)

@responses.activate
def test_pause_conflict_error():
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_json = {
        'code': 'harmony.ConflictError',
        'description': 'Error: Job status cannot be updated from successful to paused.'
    }

    responses.add(
        responses.GET,
        expected_pause_url(job_id),
        status=409,
        json = exp_json
    )

    with pytest.raises(Exception) as e:
        Client(should_validate_auth=False).pause(job_id)
    assert str(e.value) == "('Conflict', 'Error: Job status cannot be updated from successful to paused.')"

@responses.activate
def test_resume():
    collection = Collection(id='C333666999-EOSDIS')
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_job = expected_paused_job(collection, job_id)
    responses.add(
        responses.GET,
        expected_resume_url(job_id),
        status=200,
        json=exp_job
    )

    Client(should_validate_auth=False).resume(job_id)

    assert len(responses.calls) == 1
    assert responses.calls[0].request is not None
    assert urllib.parse.unquote(responses.calls[0].request.url) == expected_resume_url(job_id)

@responses.activate
def test_resume_conflict_error():
    job_id = '21469294-d6f7-42cc-89f2-c81990a5d7f4'
    exp_json = {
        'code': 'harmony.ConflictError',
        'description': 'Error: Job status is running - only paused jobs can be resumed.'
    }

    responses.add(
        responses.GET,
        expected_resume_url(job_id),
        status=409,
        json = exp_json
    )

    with pytest.raises(Exception) as e:
        Client(should_validate_auth=False).resume(job_id)
    assert str(e.value) == "('Conflict', 'Error: Job status is running - only paused jobs can be resumed.')"

@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
def test_wait_for_processing_with_show_progress(mocker, show_progress):
    expected_progress = [
        (80, 'running', 'The job is being processed'),
        (90, 'running', 'The job is being processed'),
        (100, 'successful', 'The job was successful'),
    ]
    job_id = '12345'

    progressbar_mock = mocker.Mock()
    progressbar_mock.__enter__ = lambda _: progressbar_mock
    progressbar_mock.__exit__ = lambda a, b, d, c: None
    mocker.patch('harmony.harmony.progressbar.ProgressBar', return_value=progressbar_mock)

    sleep_mock = mocker.Mock()
    mocker.patch('harmony.harmony.time.sleep', sleep_mock)

    progress_mock = mocker.Mock(side_effect=expected_progress)
    mocker.patch('harmony.harmony.Client.progress', progress_mock)

    client = Client(should_validate_auth=False)
    client.wait_for_processing(job_id, show_progress=show_progress)

    progress_mock.assert_called_with(job_id)
    if show_progress:
        for n, _, _ in expected_progress:
            progressbar_mock.update.assert_any_call(int(n))
    else:
        assert sleep_mock.call_count == len(expected_progress)


@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
def test_wait_for_processing_with_failed_status(mocker, show_progress):
    expected_progress = [(0, 'failed', 'Pod exploded')]
    job_id = '12345'

    progressbar_mock = mocker.Mock()
    progressbar_mock.__enter__ = lambda _: progressbar_mock
    progressbar_mock.__exit__ = lambda a, b, d, c: None
    mocker.patch('harmony.harmony.progressbar.ProgressBar', return_value=progressbar_mock)

    progress_mock = mocker.Mock(side_effect=expected_progress)
    mocker.patch('harmony.harmony.Client.progress', progress_mock)

    client = Client(should_validate_auth=False)

    with pytest.raises(ProcessingFailedException) as e:
        client.wait_for_processing(job_id, show_progress=show_progress)
    assert e.exconly() == 'harmony.harmony.ProcessingFailedException: Pod exploded'

@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
def test_wait_for_processing_with_paused_status(mocker, show_progress):
    expected_progress = [
        (10, 'running', 'The job is being processed'),
        (10, 'paused', 'Job paused')]
    job_id = '12345'

    sleep_mock = mocker.Mock()
    mocker.patch('harmony.harmony.time.sleep', sleep_mock)

    progressbar_mock = mocker.Mock()
    progressbar_mock.__enter__ = lambda _: progressbar_mock
    progressbar_mock.__exit__ = lambda a, b, d, c: None
    mocker.patch('harmony.harmony.progressbar.ProgressBar', return_value=progressbar_mock)

    progress_mock = mocker.Mock(side_effect=expected_progress)
    mocker.patch('harmony.harmony.Client.progress', progress_mock)

    client = Client(should_validate_auth=False)
    client.wait_for_processing(job_id, show_progress=show_progress)

    progress_mock.assert_called_with(job_id)
    if show_progress:
        for n, _, _ in expected_progress:
            progressbar_mock.update.assert_any_call(int(n))
    else:
        # sleep should be called just once since the second status update returned 'paused'
        assert sleep_mock.call_count == 1

@responses.activate
@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_result_json(mocker, show_progress, link_type):
    expected_json = '{}'
    job_id = '1234'

    wait_mock = mocker.Mock()
    mocker.patch('harmony.harmony.Client.wait_for_processing', wait_mock)

    responses.add(
        responses.GET,
        expected_status_url(job_id, link_type),
        status=200,
        json=expected_json
    )
    client = Client(should_validate_auth=False)
    actual_json = client.result_json(job_id, show_progress=show_progress, link_type=link_type)

    assert actual_json == expected_json
    wait_mock.assert_called_with(job_id, show_progress)


@responses.activate
@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_result_json_with_failed_request_doesnt_throw_exception(mocker, show_progress, link_type):
    expected_json = '{"status": "failed", "message": "Pod exploded"}'
    job_id = '1234'

    wait_mock = mocker.Mock(side_effect=ProcessingFailedException(job_id, "Pod exploded"))
    mocker.patch('harmony.harmony.Client.wait_for_processing', wait_mock)

    responses.add(
        responses.GET,
        expected_status_url(job_id),
        status=200,
        json=expected_json
    )
    client = Client(should_validate_auth=False)
    actual_json = client.result_json(job_id, show_progress=show_progress)

    assert actual_json == expected_json
    wait_mock.assert_called_with(job_id, show_progress)


@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_result_urls(mocker, show_progress, link_type):
    collection = Collection(id='C1940468263-POCLOUD')
    job_id = '1234'
    expected_json = expected_job(collection.id, job_id, link_type)
    expected_urls = [fake_data_url(link_type)]

    result_json_mock = mocker.Mock(return_value=expected_json)
    processing_mock = mocker.Mock(return_value=None)
    mocker.patch('harmony.harmony.Client._get_json', result_json_mock)
    mocker.patch('harmony.harmony.Client.wait_for_processing', processing_mock)

    client = Client(should_validate_auth=False)
    actual_urls = list(client.result_urls(job_id, show_progress=show_progress, link_type=link_type))

    assert actual_urls == expected_urls
    result_json_mock.assert_called_with(f'https://harmony.earthdata.nasa.gov/jobs/{job_id}?linktype={link_type.value}')


@pytest.mark.parametrize('show_progress', [
    (True),
    (False),
])
@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_result_url_paging(mocker, show_progress, link_type):
    collection = Collection(id='C1940468263-POCLOUD')
    job_id = '1234'
    next_link = {
        'href': f'https://harmony.earthdata.nasa.gov/jobs/{job_id}?linktype={link_type.value}&page=2',
        'rel': 'next',
    }
    expected_json_first_page = expected_job(collection.id, job_id, link_type, [next_link])
    expected_json_last_page = expected_job(collection.id, job_id, link_type)
    expected_urls = [fake_data_url(link_type), fake_data_url(link_type)]

    get_json_mock = mocker.Mock(side_effect=[expected_json_first_page, expected_json_last_page])
    processing_mock = mocker.Mock(return_value=None)
    mocker.patch('harmony.harmony.Client._get_json', get_json_mock)
    mocker.patch('harmony.harmony.Client.wait_for_processing', processing_mock)

    client = Client(should_validate_auth=False)
    actual_urls = list(client.result_urls(job_id, show_progress=show_progress, link_type=link_type))

    assert actual_urls == expected_urls
    get_json_mock.assert_any_call(f'https://harmony.earthdata.nasa.gov/jobs/{job_id}?linktype={link_type.value}')
    get_json_mock.assert_any_call(f'https://harmony.earthdata.nasa.gov/jobs/{job_id}?linktype={link_type.value}&page=2')


@pytest.mark.parametrize('overwrite', [
    (True),
    (False),
])
def test_download_file(overwrite):
    # On first iteration, the local file is created with 'incorrect' data
    #   - overwrite is True so local file is overwritten with expected_data
    #   - assert filename and data are correct
    # On second iteration, the local file is not overwritten
    #   - AssertionError is thrown by pytest responses because no GET is actually performed
    #   - assert filename and data are still correct
    #   - local test file is deleted
    expected_data = bytes('abcde', encoding='utf-8')
    unexpected_data = bytes('vwxyz', encoding='utf-8')
    expected_filename = 'pytest_tempfile.temp'
    url = 'http://example.com/' + expected_filename
    actual_output = None

    with io.BytesIO() as file_obj:
        file_obj.write(expected_data)
        file_obj.seek(0)

        if overwrite:
            with open(expected_filename, 'wb') as f:
                f.write(unexpected_data)

            with responses.RequestsMock() as resp_mock:
                resp_mock.add(responses.GET, url, body=file_obj.read(), stream=True)
                client = Client(should_validate_auth=False)
                actual_output = client._download_file(url, overwrite=overwrite)
        else:
            with pytest.raises(AssertionError):
                # throws AssertionError because requests GET is never actually called here
                with responses.RequestsMock() as resp_mock:
                    resp_mock.add(responses.GET, url, body=file_obj.read(), stream=True)
                    client = Client(should_validate_auth=False)
                    actual_output = client._download_file(url, overwrite=overwrite)

    assert actual_output == expected_filename
    with open(expected_filename, 'rb') as temp_file:
        data = temp_file.read()
        assert data == expected_data

    if not overwrite:
        os.unlink(expected_filename)


def test_download_all(mocker):
    expected_urls = [
        'http://www.example.com/1',
        'http://www.example.com/2',
        'http://www.example.com/3',
    ]
    expected_file_names = ['1', '2', '3']

    result_urls_mock = mocker.Mock(return_value=expected_urls)
    mocker.patch('harmony.harmony.Client.result_urls', result_urls_mock)
    mocker.patch(
        'harmony.harmony.Client._download_file',
        lambda self, url, a, b: url.split('/')[-1]
    )

    client = Client(should_validate_auth=False)
    actual_file_names = [f.result() for f in client.download_all('abcd-1234')]

    assert actual_file_names == expected_file_names

def side_effect_func_for_download_file(url: str, directory: str = '', overwrite: bool = False) -> str:
    filename = url.split('/')[-1]
    return os.path.join(directory, filename)

# list of links to provide on subsequent calls to _get_json
def extra_links_for_iteration(link_type: str):
    return [
        {
            'href': f'{link_type}://harmony.earthdata.nasa.gov/service-results/fake2.tif',
            'title': '2020_01_15_fake.nc.tif',
            'type': 'image/tiff',
            'rel': 'data',
            'bbox': [
                -9.95,
                -8.95,
                19.95,
                8.95
            ],
            'temporal': {
                'start': '2020-02-15T00:00:00.000Z',
                'end': '2020-02-15T23:59:59.000Z'
            }
        },
        {
            'href': f'{link_type}://harmony.earthdata.nasa.gov/service-results/fake3.tif',
            'title': '2020_01_15_fake.nc.tif',
            'type': 'image/tiff',
            'rel': 'data',
            'bbox': [
                -109.95,
                -18.95,
                29.95,
                11.95
            ],
            'temporal': {
                'start': '2020-03-15T00:00:00.000Z',
                'end': '2020-03-15T23:59:59.000Z'
            }
        },
        {
            'href': f'{link_type}://harmony.earthdata.nasa.gov/service-results/fake4.tif',
            'title': '2020_01_15_fake.nc.tif',
            'type': 'image/tiff',
            'rel': 'data',
            'bbox': [
                -79.95,
                -38.95,
                1.95,
                18.95
            ],
            'temporal': {
                'start': '2020-04-15T00:00:00.000Z',
                'end': '2020-04-15T23:59:59.000Z'
            }
        },
        {
            'href': f'{link_type}://harmony.earthdata.nasa.gov/service-results/fake5.tif',
            'title': '2020_01_15_fake.nc.tif',
            'type': 'image/tiff',
            'rel': 'data',
            'bbox': [
                -49.95,
                -28.95,
                39.95,
                14.95
            ],
            'temporal': {
                'start': '2020-05-15T00:00:00.000Z',
                'end': '2020-05-15T23:59:59.000Z'
            }
        },
        {
            'href': f'{link_type}://harmony.earthdata.nasa.gov/service-results/fake6.tif',
            'title': '2020_01_15_fake.nc.tif',
            'type': 'image/tiff',
            'rel': 'data',
            'bbox': [
                -44.95,
                -21.95,
                19.95,
                17.95
            ],
            'temporal': {
                'start': '2020-06-15T00:00:00.000Z',
                'end': '2020-06-15T23:59:59.000Z'
            }
        },
    ]

# this function provides a different value for subsequent calls to _get_json to simulate
# changing status page
def side_effect_for_get_json(extra_links) -> List[str]:
    status_running1 = expected_job('C123', 'abc123')
    status_running1['links'].append(extra_links[0])
    status_running2 = copy.deepcopy(status_running1)
    status_running2['links'].append(extra_links[1])
    status_paused = copy.deepcopy(status_running2)
    status_paused['status'] = 'paused'
    status_paused['links'].append(extra_links[2])
    status_resumed = copy.deepcopy(status_paused)
    status_resumed['links'].append(extra_links[3])
    status_resumed['status'] = 'running'
    status_successful = copy.deepcopy(status_resumed)
    status_successful['links'].append(extra_links[4])
    status_successful['status'] = 'successful'
    
    return [status_running1, status_running2, status_paused, status_resumed, status_successful, status_successful]

@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_iterator(link_type, mocker):
    extra_links = extra_links_for_iteration(link_type.value)
    status_page_json = expected_job('C123', 'abc123')
    status_page_json['status'] = 'successful'
    download_file_mock = mocker.Mock(side_effect = side_effect_func_for_download_file)
    mocker.patch('harmony.harmony.Client._download_file', download_file_mock)
    get_json_mock = mocker.Mock(side_effect = side_effect_for_get_json(extra_links))
    mocker.patch('harmony.harmony.Client._get_json', get_json_mock)
    # speed up test by not waiting between polling the status page
    client = Client(should_validate_auth=False, check_interval=0)

    # first iteration in which job state is 'running' and two granules have completed
    iter = client.iterator(status_page_json['jobID'], '/tmp')
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(-179.95,-89.95,179.95,89.95)
    assert granule_data['path'].result() == '/tmp/fake.tif'

    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[0]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake2.tif'

    # job is still running and has another completed granule 
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[1]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake3.tif'

    # job is paused and has another completed granule
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[2]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake4.tif'

    # once the new granules are downloaded for the paused job the iterator stops
    granule_data = next(iter, None)
    assert granule_data == None

    # need to start a new iterator after resuming
    iter = client.iterator(status_page_json['jobID'], '/tmp')
    # initial granules returned are the same as before (they are not re-downloaded by default)
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(-179.95,-89.95,179.95,89.95)
    assert granule_data['path'].result() == '/tmp/fake.tif'
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[0]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake2.tif'
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[1]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake3.tif'
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[2]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake4.tif'
    
    # job is running and has another completed granule
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[3]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake5.tif'

    # job has succeeded and has another completed granule
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[4]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake6.tif'

    # iterator completes after job is in a final state and there are no new granules
    granule_data = next(iter, None)
    assert granule_data == None

# this function provides a different value for subsequent calls to _get_json to simulate
# changing status page - in this case the status changes from 'running' to 'failed'
def side_effect_for_get_json_failed_job(extra_links) -> List[str]:
    status_running = expected_job('C123', 'foo123')
    status_running['links'].append(extra_links[0])
    status_failed = copy.deepcopy(status_running)
    status_failed['status'] = 'failed'
    status_failed['message'] = 'Job failed'
    
    return [status_running, status_failed]

@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_iterator_failed_job(link_type, mocker):
    # test with two successful work items followed by a failed job
    extra_links = extra_links_for_iteration(link_type.value)
    get_json_mock = mocker.Mock(side_effect = 
        side_effect_for_get_json_failed_job(extra_links=extra_links))
    mocker.patch('harmony.harmony.Client._get_json', get_json_mock)
    client = Client(should_validate_auth=False, check_interval=0)

    iter = client.iterator('foo123', '/tmp')
    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(-179.95,-89.95,179.95,89.95)
    assert granule_data['path'].result() == '/tmp/fake.tif'

    granule_data = next(iter)
    assert granule_data['bbox'] == BBox(*extra_links[0]['bbox'])
    assert granule_data['path'].result() == '/tmp/fake2.tif'
    
    with pytest.raises(Exception) as exc_info:
        granule_data = next(iter)
    assert str(exc_info.value) == 'Job failed'

def side_effect_func_for_get_json_with_error(url: str):
    raise Exception('something bad happened')

def test_iterator_retry(mocker):
    os.environ['GET_JSON_RETRY_SLEEP'] = '0'
    os.environ['GET_JSON_RETRY_LIMIT'] = '2'
    get_json_mock = mocker.Mock(side_effect = side_effect_func_for_get_json_with_error)
    mocker.patch('harmony.harmony.Client._get_json', get_json_mock)
    client = Client(should_validate_auth=False)

    # first iteration in which job state is 'running' and two granules have completed
    iter = client.iterator('foo', '/tmp')
    with pytest.raises(Exception) as exc_info:
        next(iter)
    assert str(exc_info.value) == 'Failed to get or parse job status page'
    assert get_json_mock.call_count == 2

@pytest.mark.parametrize('link_type', [LinkType.http, LinkType.https, LinkType.s3])
def test_stac_catalog_url(link_type, mocker):
    job_id = '1234'
    collection = Collection(id='C1940468263-POCLOUD')
    expected_json = expected_job(collection.id, job_id)
    result_json_mock = mocker.Mock(return_value=expected_json)
    mocker.patch('harmony.harmony.Client.result_json', result_json_mock)

    expected_stac_catalog_url = (f'https://harmony.earthdata.nasa.gov/stac'
                                 f'/{job_id}/?linktype={link_type.value}')

    client = Client(should_validate_auth=False)
    actual_stac_catalog_url = client.stac_catalog_url(job_id, link_type=link_type)

    assert actual_stac_catalog_url == expected_stac_catalog_url


@responses.activate
def test_read_text(mocker):
    url = 'http://www.example.com/1234'
    expected_text = '5678'
    responses.add(
        responses.GET,
        url,
        status=200,
        body=expected_text
    )

    client = Client(should_validate_auth=False)
    actual_text = client.read_text(url)

    assert actual_text == expected_text


def test_request_as_curl_get():
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        spatial=BBox(-107, 40, -105, 42)
    )

    curl_command = Client(should_validate_auth=False).request_as_curl(request)
    assert f'https://harmony.earthdata.nasa.gov/{collection.id}' \
           f'/ogc-api-coverages/1.0.0/collections/all/coverage/rangeset' in curl_command
    assert '-X GET' in curl_command


def test_request_as_curl_post(examples_dir):
    collection = Collection(id='C1940468263-POCLOUD')
    request = Request(
        collection=collection,
        shape=os.path.join(examples_dir, 'asf_example.json'),
        spatial=BBox(-107, 40, -105, 42)
    )

    curl_command = Client(should_validate_auth=False).request_as_curl(request)
    assert f'https://harmony.earthdata.nasa.gov/{collection.id}' \
           f'/ogc-api-coverages/1.0.0/collections/all/coverage/rangeset' in curl_command
    assert '-X POST' in curl_command
