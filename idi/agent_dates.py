"""Server-owned dates for conversational reads; never infer the clock from history."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .db import now
from .domain import DomainError

CHINA = timezone(timedelta(hours=8))
PERIODS = ('today', 'yesterday', 'day_before_yesterday', 'this_week', 'last_week', 'this_month', 'last_month')
DATE_PERIOD = {'date_period': {'type': 'string', 'enum': list(PERIODS),
    'description': '相对消息发送日的北京时间范围，由服务器计算；今天用 today，昨天用 yesterday，勿自行猜年份'}}
CASE_DATE_LABELS = {'report_at': '报案日期', 'created_at': '首次入库日期', 'updated_at': '台账更新日期'}
WORDS = {'今天': 'today', '今日': 'today', '昨天': 'yesterday', '昨日': 'yesterday', '前天': 'day_before_yesterday',
         '本周': 'this_week', '这周': 'this_week', '上周': 'last_week', '本月': 'this_month', '这个月': 'this_month', '上月': 'last_month', '上个月': 'last_month'}
DATE_PATTERN = r'(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})日?'
GUIDANCE = '''当前日期只以服务器 time_context 为准，历史答复中的“今天”不能作为本次日期。今天/昨天等相对日期按本次消息发送时的北京时间解释，使用 date_period，由程序计算范围；不得猜测年份。
查询新增报案按报案日期 report_at；新增入库按 created_at；台账变化按 updated_at。查询新照片按本机归档日期。日期缺失和官网快照未实时刷新须明确说明；查到0条仅代表所列范围和口径，没有日期证据不能声称当天没有新增。'''


def parsed(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (result if result.tzinfo else result.replace(tzinfo=CHINA)).astimezone(CHINA)
    except (ValueError, TypeError, OverflowError):
        return None


def local_date(value):
    result = parsed(value)
    return result.date().isoformat() if result else None


class AgentDates:
    def __init__(self, task):
        self.current = parsed(now())
        data = task.get('input', {})
        sent = None
        try:
            if data.get('source_time'):
                sent = datetime.fromtimestamp(int(data['source_time']) / 1000, CHINA)
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        self.anchor_source = 'message_sent_at' if sent else 'message_received_at'
        self.anchor = sent or parsed(data.get('captured_at')) or parsed(task.get('created_at')) or self.current
        if not sent and not data.get('captured_at') and not task.get('created_at'):
            self.anchor_source = 'server_time_fallback'
        self.text = data.get('text', '')
        # A quoted past question or an explicit historical date is not an implicit
        # instruction to constrain every lookup to today's date.
        unquoted = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"', '', self.text)
        self.periods = list(dict.fromkeys(WORDS[w] for w in re.findall('|'.join(WORDS), unquoted)))
        if re.search(DATE_PATTERN, unquoted):
            self.periods = []
        self.case_basis = None
        if re.search(r'入库|录入|导入|建档', unquoted):
            self.case_basis = 'created_at'
        elif re.search(r'更新|变更|修改', unquoted):
            self.case_basis = 'updated_at'
        elif '报案' in unquoted:
            self.case_basis = 'report_at'
        elif re.search(r'新增.*案件|新案件', unquoted):
            self.case_basis = 'created_at'
        self.required = set()
        date_query = bool(re.search(r'有|哪些|多少|查询|查看|查一下|统计|汇总|列出|新增|新照片|新图片|收到', unquoted))
        if self.periods and task.get('kind') == 'chat' and date_query:
            if re.search(r'照片|图片|收图', unquoted):
                self.required.add('search_photos')
            if re.search(r'报案|入库|新增.*案件|新案件|台账.*更新|更新.*台账', unquoted) and (not self.required or re.search(r'新增.*报案|新报案', unquoted)):
                self.required.add('search_cases')
        self.reads = []

    def interval(self, period):
        day = self.anchor.date()
        if period in PERIODS[:3]:
            start = end = day - timedelta(days=PERIODS.index(period))
        elif period in ('this_week', 'last_week'):
            start = day - timedelta(days=day.weekday() + (7 if period == 'last_week' else 0))
            end = start + timedelta(days=6)
        else:
            start = day.replace(day=1)
            if period == 'last_month':
                end = start - timedelta(days=1)
                start = end.replace(day=1)
            else:
                end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return {'date_from': start.isoformat(), 'date_to': end.isoformat()}

    def public(self):
        return {'timezone': 'Asia/Shanghai', 'server_now': self.current.isoformat(timespec='seconds'),
                'message_at': self.anchor.isoformat(timespec='seconds'), 'anchor_source': self.anchor_source,
                'message_date': self.anchor.date().isoformat(),
                'relative_dates': {p: self.interval(p) for p in PERIODS},
                'requested_periods': self.periods, 'case_date_basis': self.case_basis}

    def system_prompt(self):
        return (GUIDANCE + f'\n服务器北京时间：{self.current.isoformat(timespec="seconds")}；本次消息时间：'
                f'{self.anchor.isoformat(timespec="seconds")}；相对日期基准日：{self.anchor.date().isoformat()}。')

    def resolve(self, name, args):
        result = dict(args)
        period = args.get('date_period')
        if name in self.required:
            if period and period not in self.periods:
                raise DomainError('相对日期与本次问题不一致，请使用 ' + '、'.join(self.periods))
            if not period:
                if len(self.periods) == 1:
                    period = self.periods[0]
                else:
                    period = next((p for p in self.periods if all(args.get(k) == v for k, v in self.interval(p).items())), None)
                    if not period:
                        raise DomainError('本次问题有多个日期范围，请分别使用 date_period 查询：' + '、'.join(self.periods))
        if period:
            expected = self.interval(period)
            if any(args.get(k) and args[k] != v for k, v in expected.items()):
                raise DomainError(f'日期与消息北京时间不符：{period} 应为 {expected["date_from"]} 至 {expected["date_to"]}；请用 date_period 重新查询')
            result.update(expected, date_period=period)
        if name == 'search_cases':
            basis = args.get('date_basis') or self.case_basis or 'updated_at'
            if name in self.required and self.case_basis and basis != self.case_basis:
                raise DomainError('本次问题应按' + CASE_DATE_LABELS[self.case_basis] + '查询，date_basis=' + self.case_basis)
            result['date_basis'] = basis
        return result

    def record(self, name, args, result):
        evidence = {'tool': name, 'date_from': args.get('date_from'), 'date_to': args.get('date_to'),
                    'date_period': args.get('date_period'), 'date_basis': args.get('date_basis', 'created_at'),
                    'timezone': 'Asia/Shanghai', 'total': result['total'],
                    'unknown_date_count': result.get('unknown_date_count', 0)}
        result['date_filter'] = evidence.copy()
        self.reads.append(evidence)

    def finish(self, text, limit=1800):
        for name in self.required:
            for period in self.periods:
                if not any(r['tool'] == name and r['date_period'] == period for r in self.reads):
                    raise DomainError(f'尚未按消息北京时间查询 {period}，请先成功调用 {name}，不能据错误日期或历史答复断言没有新增')
        for word, year, month, day in re.findall(r'(' + '|'.join(WORDS) + r')\s*(?:[（(：:]\s*|是\s*)?' + DATE_PATTERN, text):
            expected = self.interval(WORDS[word])
            value = f'{int(year):04d}-{int(month):02d}-{int(day):02d}'
            if not expected['date_from'] <= value <= expected['date_to']:
                raise DomainError(f'答复中的“{word}”日期不符，应为 {expected["date_from"]} 至 {expected["date_to"]}')
        if self.required:
            lines = []
            for r in self.reads:
                if r['tool'] not in self.required or r['date_period'] not in self.periods:
                    continue
                span = r['date_from'] if r['date_from'] == r['date_to'] else r['date_from'] + ' 至 ' + r['date_to']
                label = CASE_DATE_LABELS[r['date_basis']] if r['tool'] == 'search_cases' else '照片归档日期'
                line = f'查询口径：北京时间 {span}，按{label}。'
                if r['tool'] == 'search_cases':
                    line += '依据当前授权范围的本机台账及官网已同步快照，未实时刷新官网。'
                else:
                    line += '范围为当前授权项目的本机归档照片。'
                if r['unknown_date_count']:
                    line += f'另有 {r["unknown_date_count"]} 件日期缺失或无效，未纳入日期统计。'
                if line not in lines:
                    lines.append(line)
            suffix = '\n\n' + '\n'.join(lines)
            text = text[:max(0, limit - len(suffix))] + suffix
        return text[:limit]
