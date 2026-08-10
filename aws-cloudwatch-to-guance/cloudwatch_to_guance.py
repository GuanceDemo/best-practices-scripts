"""Forward AWS CloudWatch Alarm notifications from SNS to Guance.

Runtime: DataFlux Func
Entry: receive(**data)
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request


TOPIC = DFF.ENV('AWS_SNS_TOPIC_ARN')
SNS_HOST = DFF.ENV('AWS_SNS_HOST')
GUANCE_URL = DFF.ENV('GUANCE_EXTERNAL_EVENT_URL')

STATUS = {
    'ALARM': 'error',
    'OK': 'ok',
    'INSUFFICIENT_DATA': 'warning',
}

CHECK_VALUE = {
    'ALARM': 1,
    'OK': 0,
    'INSUFFICIENT_DATA': -1,
}


def _require_config():
    missing = []
    if not TOPIC:
        missing.append('AWS_SNS_TOPIC_ARN')
    if not SNS_HOST:
        missing.append('AWS_SNS_HOST')
    if not GUANCE_URL:
        missing.append('GUANCE_EXTERNAL_EVENT_URL')
    if missing:
        raise RuntimeError(
            'missing Func environment variables: {}'.format(', '.join(missing))
        )


def _resp(data, code=200):
    return DFF.RESP(data, status_code=code, content_type='json')


def _payload(data):
    value = data.get('text', data)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError('request body is not valid JSON')

    if not isinstance(value, dict):
        raise ValueError('request body must be a JSON object')

    if value.get('TopicArn') != TOPIC:
        raise PermissionError('unexpected SNS TopicArn')
    return value


def _confirm(value):
    url = value.get('SubscribeURL', '')
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    valid = (
        parsed.scheme == 'https'
        and parsed.hostname == SNS_HOST
        and query.get('Action') == ['ConfirmSubscription']
        and query.get('TopicArn') == [TOPIC]
        and bool(query.get('Token'))
    )
    if not valid:
        raise PermissionError('invalid SNS SubscribeURL')

    with urllib.request.urlopen(url, timeout=8) as response:
        response.read(2048)
        return response.status


def _tag_key(value):
    key = re.sub(
        r'[^a-zA-Z0-9_]+',
        '_',
        str(value),
    ).strip('_').lower()
    return key[:64] or 'dimension'


def _text(value, default='unknown'):
    return str(value if value not in (None, '') else default)[:256]


def _event(alarm, sns):
    state = _text(alarm.get('NewStateValue'), 'UNKNOWN').upper()
    trigger = alarm.get('Trigger') or {}
    name = _text(alarm.get('AlarmName'), 'Unnamed CloudWatch Alarm')

    tags = {
        'source': 'aws_cloudwatch',
        'alarm_name': name,
        'cloudwatch_state': state,
        'aws_account_id': _text(alarm.get('AWSAccountId')),
        'aws_region': _text(alarm.get('Region')),
        'aws_namespace': _text(trigger.get('Namespace')),
        'aws_metric_name': _text(trigger.get('MetricName')),
    }

    for item in trigger.get('Dimensions') or []:
        if isinstance(item, dict) and item.get('name'):
            tags[_tag_key(item['name'])] = _text(item.get('value'))

    reason = str(
        alarm.get('NewStateReason')
        or 'CloudWatch alarm state changed'
    )
    message = (
        '{}\n\n'
        '状态变化时间：{}\n'
        '区域：{}\n'
        '指标：{}/{}'
    ).format(
        reason,
        alarm.get('StateChangeTime')
        or sns.get('Timestamp')
        or 'unknown',
        alarm.get('Region') or 'unknown',
        trigger.get('Namespace') or 'unknown',
        trigger.get('MetricName') or 'unknown',
    )

    dimension_lines = [
        '- {}: {}'.format(key, tags[key])
        for key in sorted(tags)
    ]
    message += '\n\n事件维度：\n' + '\n'.join(dimension_lines)

    return {
        'event': {
            'status': STATUS.get(state, 'warning'),
            'title': '[AWS CloudWatch][{}] {}'.format(state, name)[:256],
            'message': message,
            'dimension_tags': tags,
            'check_value': CHECK_VALUE.get(state, -1),
        },
        'extraData': {
            'source': 'aws_cloudwatch',
            'alarmName': name,
            'alarmArn': alarm.get('AlarmArn'),
            'oldStateValue': alarm.get('OldStateValue'),
            'newStateValue': state,
            'stateChangeTime': alarm.get('StateChangeTime'),
            'snsMessageId': sns.get('MessageId'),
            'topicArn': sns.get('TopicArn'),
            'trigger': trigger,
        },
    }


def _push(event):
    request = urllib.request.Request(
        GUANCE_URL,
        data=json.dumps(event, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={
            'Content-Type': 'application/json;charset=UTF-8',
            'Accept': 'application/json',
            'User-Agent': 'DataFlux-Func-CloudWatch-Bridge/1.0',
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read(8192).decode('utf-8', errors='replace')
            return response.status, body
    except urllib.error.HTTPError as error:
        body = error.read(8192).decode('utf-8', errors='replace')
        print('Guance external event HTTP error:', error.code, body)
        raise


@DFF.API(
    'AWS CloudWatch 告警转观测云外部事件',
    category='webhook',
    tags=['aws', 'sns', 'cloudwatch', 'guance', 'webhook'],
    timeout=20,
)
def receive(**data):
    """Receive one SNS request and forward CloudWatch Alarm events."""
    try:
        _require_config()
        sns = _payload(data)
        message_type = sns.get('Type')

        if message_type == 'SubscriptionConfirmation':
            status = _confirm(sns)
            print('AWS SNS subscription confirmed:', TOPIC)
            return _resp({
                'ok': True,
                'confirmed': True,
                'snsStatus': status,
            })

        if message_type == 'Notification':
            alarm = sns.get('Message')
            if isinstance(alarm, str):
                try:
                    alarm = json.loads(alarm)
                except (TypeError, ValueError):
                    raise ValueError(
                        'SNS Message is not CloudWatch Alarm JSON'
                    )
            if not isinstance(alarm, dict):
                raise ValueError('SNS Message is not CloudWatch Alarm JSON')

            event = _event(alarm, sns)
            print('Guance external event payload:', event)
            status, body = _push(event)
            print('Guance external event response:', status, body)
            return _resp({
                'ok': True,
                'alarmName': alarm.get('AlarmName'),
                'alarmState': alarm.get('NewStateValue'),
                'guanceStatus': status,
            })

        if message_type == 'UnsubscribeConfirmation':
            return _resp({'ok': True, 'type': message_type})

        return _resp({
            'ok': False,
            'error': 'unsupported SNS message Type',
        }, 400)

    except PermissionError as error:
        print('AWS SNS request rejected:', str(error))
        return _resp({'ok': False, 'error': str(error)}, 403)
    except ValueError as error:
        print('AWS SNS payload rejected:', str(error))
        return _resp({'ok': False, 'error': str(error)}, 400)
    except Exception as error:
        print('SNS to Guance bridge failed:', repr(error))
        return _resp({
            'ok': False,
            'error': 'SNS to Guance bridge failed',
        }, 500)
