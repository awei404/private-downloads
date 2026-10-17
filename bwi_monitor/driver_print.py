"""Local, read-only Letter check-in slip. Never fills in an unrecorded arrival."""
from html import escape

ADDRESSES = {
    'ORF01': '30 Aberdeen Rd, Hampton, VA 23661',
    'RIC01': '1103 Oliver Hl Wy, Richmond, VA 23219',
    'DCA01': '10726 Tucker St, Unit B, Beltsville, MD 20705',
    'IAD01': '6714 Electronic Dr, Springfield, VA 22151',
}


def render(task, segment):
    if not task or not task.get('in_scope') or task.get('route') not in ADDRESSES:
        raise ValueError('仅可打印 BWI 始发四支线任务。')
    def text(value, missing='待填写 / Not recorded'):
        return escape(str(value), quote=True) if value else missing
    def timestamp(value):
        return text(str(value).replace('T', ' ')[:16] if value else None)
    destination = task['route']
    fields = {
        'title': text(task.get('task_no')), 'day': text(task.get('op_date')),
        'route': text(destination.rstrip('0123456789')), 'address': text(ADDRESSES[destination]),
        'full_route': text(segment.get('taskName'), '—'), 'name': text(task.get('name')),
        'supplier': text(task.get('supplier'), '—'), 'plate': text(task.get('plate'), '—'),
        'actual': timestamp(task.get('actual_arrival_et')), 'scheduled': timestamp(task.get('scheduled_arrival_et')),
    }
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Check-in · %(title)s</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e9edf3;color:#111;font-family:Arial,"Microsoft YaHei",sans-serif}
.toolbar{padding:14px 20px;background:#18325b;color:white;display:flex;align-items:center;justify-content:center;gap:18px;flex-wrap:wrap}.toolbar button{font-size:17px;padding:10px 25px;border:0;border-radius:6px;cursor:pointer}.toolbar small{font-size:13px}
.paper{width:7.5in;margin:24px auto;padding:.2in .25in;background:white;box-shadow:0 3px 20px #0002}header{border-bottom:3px solid;padding-bottom:12px;text-align:center}h1{font-size:27px;margin:4px}header p{margin:7px 0;font-size:14px}.destination{text-align:center;border:4px solid;margin:16px 0;padding:14px}.destination strong{display:block;font-size:78px;line-height:1.05;letter-spacing:7px}.destination p{font-size:17px;margin:12px 0 0}.route{font-size:12px;overflow-wrap:anywhere}.return{border:3px solid;padding:14px;text-align:center}.return label{font-weight:bold;display:block;font-size:17px;margin-bottom:8px}.return select{font:700 22px Arial,sans-serif;border:1px solid #888;padding:7px;width:100%%;text-align:center;background:white}.print-return{display:none;font-size:24px;font-weight:bold}.times{display:flex;gap:12px;margin:15px 0}.time{border:2px solid;padding:12px;text-align:center;flex:1}.label{font-size:12px;color:#444}.value{font-size:19px;font-weight:bold;margin-top:8px;overflow-wrap:anywhere}.info{display:grid;grid-template-columns:1fr 1fr;gap:13px 18px;border-top:1px solid #999;padding-top:14px}.info .value{font-size:18px}input,textarea{font:17px Arial,sans-serif;border:0;border-bottom:1px solid #777;padding:5px 0;width:100%%;background:transparent;resize:none}textarea{height:68px}.notes{margin-top:13px}.ack{margin-top:18px;border-top:2px solid;padding-top:11px;break-inside:avoid}.ack h2{font-size:15px;margin:0 0 8px}.ack p{font-size:12px;line-height:1.5;margin:5px 0}.signatures{display:flex;gap:30px;margin-top:33px}.signatures div{flex:1;border-top:1px solid;padding-top:7px;font-size:11px}.foot{font-size:10px;color:#555;text-align:center;margin-top:13px}.print-note{display:none;white-space:pre-wrap;overflow-wrap:anywhere;min-height:42px;font-size:16px}
@page{size:Letter portrait;margin:.45in}@media print{body{background:white}.toolbar{display:none}.paper{width:auto;margin:0;padding:0;box-shadow:none}.return select,.notes textarea{display:none}.print-return,.print-note{display:block}.destination,.return,.times,.info{break-inside:avoid}input{border-bottom:1px solid #777}.paper{print-color-adjust:exact;-webkit-print-color-adjust:exact}}@media screen and (max-width:760px){.paper{width:95%%}.destination strong{font-size:64px}}
</style></head><body>
<div class="toolbar"><button id="printButton" type="button">打印 / 保存 PDF</button><small>Dock、回货要求和备注仅用于本次打印，不修改登记或评分。</small></div>
<main class="paper"><header><h1>BWIH Driver Check-in</h1><p>作业日 / Operating day: %(day)s &nbsp; · &nbsp; %(title)s</p></header>
<section class="destination"><div class="label">首站目的地 / FIRST DESTINATION</div><strong>%(route)s</strong><p>%(address)s</p><p class="route">完整线路 / Route: %(full_route)s</p></section>
<section class="return"><label for="returnCargo">回货要求 / RETURN CARGO</label><select id="returnCargo"><option value="pending">待调度确认 / CONFIRM WITH DISPATCH</option><option value="yes">需要回货 / RETURN CARGO REQUIRED</option><option value="no">不需要回货 / NO RETURN CARGO</option></select><div id="printedReturn" class="print-return">待调度确认 / CONFIRM WITH DISPATCH</div></section>
<section class="times"><div class="time"><div class="label">实际到场 / ACTUAL ARRIVAL · ET</div><div class="value">%(actual)s</div></div><div class="time"><div class="label">约定到场 / SCHEDULED ARRIVAL · ET</div><div class="value">%(scheduled)s</div></div></section>
<section class="info"><div><div class="label">司机 / DRIVER</div><div class="value">%(name)s</div></div><div><div class="label">供应商 / CARRIER</div><div class="value">%(supplier)s</div></div><div><div class="label">车牌 / VEHICLE</div><div class="value">%(plate)s</div></div><div><label class="label" for="dock">道口 / DOCK</label><input id="dock" maxlength="25" autocomplete="off" aria-label="道口 / DOCK"></div></section>
<div class="notes"><label class="label" for="notes">备注 / NOTES</label><textarea id="notes" maxlength="220" aria-label="备注 / NOTES"></textarea><div id="printedNotes" class="print-note"></div></div>
<section class="ack"><h2>司机须知确认 / DRIVER NOTICE ACKNOWLEDGEMENT</h2><p>本人确认已阅读并理解《司机须知》，并同意遵守其中的操作要求。<br>I confirm that I have read and understood the Driver Notice and agree to follow its requirements.</p><div class="signatures"><div>司机签字 / DRIVER SIGNATURE</div><div>日期及时间 / DATE &amp; TIME</div></div></section>
<p class="foot">时间均为美东时间 / All times Eastern · 实际到场以现场保存记录为准 / Saved on-site arrival record</p></main>
<script>const cargo=document.getElementById('returnCargo'),notes=document.getElementById('notes');function prepare(){document.getElementById('printedReturn').textContent=cargo.options[cargo.selectedIndex].text;document.getElementById('printedNotes').textContent=notes.value;}cargo.addEventListener('change',prepare);notes.addEventListener('input',prepare);window.addEventListener('beforeprint',prepare);document.getElementById('printButton').addEventListener('click',()=>{prepare();window.print();});</script></body></html>''' % fields).encode('utf-8')
