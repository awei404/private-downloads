"""A small, dependency-free XLSX export for scoped driver scores.

All user/TMS text is written as an inline string, never as an Excel formula.
"""
import io
import math
import re
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RETURN_LABELS = {"not_required": "无需回货", "completed": "已完成", "partial": "部分完成",
                 "failed": "未完成／未带回", "pending": "待确认"}


def _text(value):
    value = str(value if value is not None else "")
    # XML 1.0 cannot represent these control characters or isolated surrogates.
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value)
    return escape(value[:32767])


def _column(index):
    result = ""
    while index:
        index, n = divmod(index - 1, 26)
        result = chr(65 + n) + result
    return result


def _sheet(rows):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<sheetFormatPr defaultRowHeight="20"/><cols><col min="1" max="32" width="22" customWidth="1"/></cols><sheetData>']
    width = max((len(row) for row in rows), default=1)
    for row_num, row in enumerate(rows, 1):
        parts.append(f'<row r="{row_num}">')
        for col_num, value in enumerate(row, 1):
            address = f"{_column(col_num)}{row_num}"
            style = ' s="1"' if row_num == 1 else ""
            if type(value) in (int, float) and math.isfinite(value):
                parts.append(f'<c r="{address}"{style}><v>{value}</v></c>')
            else:
                parts.append(f'<c r="{address}" t="inlineStr"{style}><is><t xml:space="preserve">{_text(value)}</t></is></c>')
        parts.append('</row>')
    parts.append(f'</sheetData><autoFilter ref="A1:{_column(width)}{max(1, len(rows))}"/></worksheet>')
    return ''.join(parts)


def workbook(sheets):
    """Return an Excel workbook; sheet names and all cell data are escaped."""
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
            ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1)) + '</Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
            ''.join(f'<sheet name="{_text(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets)+1)) +
            '<Relationship Id="style" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF18325B"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0"/><xf fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i, (_, rows) in enumerate(sheets, 1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', _sheet(rows))
    return output.getvalue()


def export_scores(data, *, include_phone=False, exported_at=""):
    headers = ['作业日', '线路', '任务编号', '司机']
    if include_phone:
        headers.append('电话')
    headers += ['司机资料来源', '供应商', '车牌', '约定到场（美东）', '实际到场（美东）', 'TMS人工发车（美东）',
                '迟到分钟', '线路正确', '回货结果', '人工扣分', '扣分原因', '任务评分', '待确认事项']
    tasks = [headers]
    for task in data['tasks']:
        profile, review, arrival = task.get('profile') or {}, task.get('review') or {}, task.get('arrival') or {}
        row = [task.get('op_date'), task.get('route') or task.get('destination'), task.get('task_no'),
               task.get('driver_name') or profile.get('driver_name') or '待同步']
        if include_phone:
            row.append(task.get('driver_phone') or profile.get('driver_phone'))
        row += [profile.get('source'), task.get('supplier') or profile.get('supplier_name'),
                task.get('plate') or profile.get('vehicle_plate'), arrival.get('scheduled_arrival_et'),
                arrival.get('actual_arrival_et'), task.get('manual_departure_et'), arrival.get('late_minutes'),
                '是' if review.get('route_ok', True) else '否', RETURN_LABELS.get(review.get('return_status', 'not_required'), '待确认'),
                review.get('manual_deduction', 0), review.get('manual_reason'), task.get('score'),
                '；'.join(task.get('pending_reasons') or [])]
        tasks.append(row)
    headers = ['司机'] + (['电话'] if include_phone else []) + [
        '近30天自动平均分', '人工总分', '当前分', '已评分任务数', '待评分任务数', '人工调整原因']
    drivers = [headers]
    for driver in data['drivers']:
        row = [driver.get('name')]
        if include_phone:
            row.append(driver.get('phone'))
        row += [driver.get('auto_score'), driver.get('manual_score'), driver.get('score'),
                driver.get('scored_task_count', 0), driver.get('pending_task_count', 0),
                (driver.get('override') or {}).get('reason')]
        drivers.append(row)
    filt = data.get('filter') or {}
    notes = [['项目', '说明'], ['导出时间（美东）', exported_at],
             ['任务范围', '整个任务由 BWI.H 始发，首段前往 ORF01、RIC01、DCA01、IAD01；排除串点中途离站、已作废及有效性未确认任务。'],
             ['任务日期筛选', f"{filt.get('start', '')} 至 {filt.get('end', '')}"],
             ['线路筛选', filt.get('route') or '四支线全部'],
             ['司机评分窗口', f"{data.get('window_start', '')} 至 {data.get('window_end', '')}（含首尾，共30个作业日；美东05:00划日）"],
             ['司机汇总范围', '所选任务中的司机；自动分始终取上述30天四支线已评分任务均值，不随任务日期筛选重算。'],
             ['评分规则', '基础100；迟到扣45；错线路直接0；回货未完成扣25、部分完成扣10；再减人工扣分，最低0。'],
             ['缺失资料', '未登记实际到场、未匹配约定或回货待确认时不生成任务分；错线路明确为0。缺可靠司机身份的任务不会按姓名或车牌合并到司机均分。'],
             ['人工总分', '人工总分覆盖当前展示分，自动平均仍保留；恢复自动后取消覆盖。'],
             ['历史数据', '旧调度中按约定回填的实际到场和历史评分未混入本次现场登记及新自动平均。'],
             ['个人信息', '包含姓名；电话仅勾选后导出。无驾照照片、凭证或原始接口数据。' if include_phone else '包含姓名；不含电话、驾照照片、凭证或原始接口数据。'],
             ['同步状态', '同步中' if (data.get('sync') or {}).get('running') else (data.get('sync') or {}).get('error') or '以已缓存资料为准']]
    return workbook([('说明', notes), ('四支线任务', tasks), ('司机评分', drivers)])
