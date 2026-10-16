"""Observed Guangzhou platform contract. These are preparation rules, not write access.

Source references and remaining uncertainties are in docs/platform-fields.md.
All branching is based on the current maintenance-company pages, not insurer audit pages.
"""
from __future__ import annotations

from copy import deepcopy

VERSION = "gz-maintenance-20261015-1791776589639"
BASE_URL = "https://gzzb2021.com/iidi"
WEBSITE = "https://gzzb2021.com/idi/#/thirdPartyRepair"
IMAGE = [".jpg", ".png", ".jpeg", ".gif"]
MEDIA = IMAGE + [".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"]


def field(key, label, kind="text", required=False, **kwargs):
    return {"key": key, "label": label, "kind": kind, "required": required, **kwargs}


def files(key, label, required, extensions, *, suggest=None, **kwargs):
    return field(key, label, "files", required, extensions=extensions, suggest=suggest, **kwargs)


STAGES = {
    "intake": {
        "label": "理赔受理", "guide_page": 11,
        "endpoint": "/aimsManage/reportReceiving/maintenanceAccept", "actions": ["submit"],
        "fields": [
            field("acceptanceResult", "受理结果", "select", True,
                  options=[{"code": "1", "name": "现场查勘"}, {"code": "2", "name": "直接赔付"}, {"code": "3", "name": "疑似非保险责任"}]),
            field("modifySurveyTimeMark", "是否修改计划现场查勘时间", "select", True,
                  when={"acceptanceResult": ["1"]}, options=[{"code": "0", "name": "否"}, {"code": "1", "name": "是"}]),
            field("modifiedSurveyTime", "修改后查勘开始时间", "datetime", True,
                  when={"acceptanceResult": ["1"], "modifySurveyTimeMark": ["1"]}),
            field("modifiedSurveyTimeEnd", "修改后查勘结束时间", "datetime", True,
                  when={"acceptanceResult": ["1"], "modifySurveyTimeMark": ["1"]}),
            field("reasonDescription", "原因说明", "textarea", True),
        ],
    },
    "survey": {
        "label": "现场查勘", "guide_page": 12,
        "endpoint": "/aimsManage/siteSurvey/addOrEdit", "actions": ["save", "submit"],
        "fields": [
            files("buildingFileInfoList", "楼栋打卡图片", True, IMAGE, suggest=["survey", "building_checkin"], checkin=True),
            files("houseFileInfoList", "入户打卡图片", True, IMAGE, suggest=["survey", "door_checkin"], checkin=True),
            files("fileInfoList", "缺陷照片/视频", True, MEDIA, suggest=["survey", "problem"]),
            field("siteSurveyDate", "实际现场查勘时间", "datetime", True),
            field("defectDesc", "缺陷原因分析", "textarea", True),
            field("divisionWorksCode", "分部工程", "dictionary", True, dictionary="part_engineering", name_key="divisionWorks"),
            field("defectiveSite", "缺陷部位", "text", True),
            field("defectTypeCode", "类型", "dictionary", True, dictionary="defect_type", name_key="defectType"),
            field("repairMethod", "维修方式", "select", True, options=[{"code": "1", "name": "当天维修"}, {"code": "2", "name": "计划维修"}, {"code": "3", "name": "查勘后零注拒"}]),
            field("planRepairDate", "计划维修日期", "date", True, when={"repairMethod": ["1", "2"]},
                  hint="当天维修也需核对实际日期；不会把导入或上传当天当作维修日期。"),
            field("repairUserId", "维修人员", "personnel", True, when={"repairMethod": ["1", "2"]}),
            field("repairPersonPhone", "联系方式", "readonly", when={"repairMethod": ["1", "2"]}, hint="随所选官网维修人员带出。"),
            field("estimateRestorationCostCode", "修复预估", "dictionary", True, when={"repairMethod": ["1", "2"]}, dictionary="estimate_restoration_cost", name_key="estimateRestorationCost"),
            field("zeroRejectReason", "零注拒原因", "textarea", True, when={"repairMethod": ["3"]}),
        ],
    },
    "repair": {
        "label": "修复成果", "guide_page": 13,
        "endpoint": "/aimsManage/repairAcceptance/addOrEdit", "actions": ["save", "submit"],
        "fields": [
            files("buildingFileInfoList", "楼栋打卡照片", True, [".jpg", ".png", ".jpeg"], suggest=["work", "building_checkin"], checkin=True, max_bytes_exclusive=20 * 1024 * 1024),
            files("houseFileInfoList", "入户打卡照片", True, [".jpg", ".png", ".jpeg"], suggest=["work", "door_checkin"], checkin=True, max_bytes_exclusive=20 * 1024 * 1024),
            field("approachStartWorkDate", "进场开工时间", "datetime", True),
            field("repairCrewNumber", "进场维修人数", "integer", True, minimum=1,
                  hint="新草稿默认 1 人，可按实际进场人数修改。"),
            field("repairUnit", "维保单位", "readonly"),
            field("repairPersonName", "维修人员", "readonly"),
            field("finishWorkDate", "完工时间", "datetime", True),
            field("timeLimit", "工期（天）", "readonly", hint="按开工与完工日期计算，首尾两天都计入。"),
            field("estimateRestorationCost", "修复预估", "readonly"),
            files("preRepairFileInfo", "缺陷照片/视频（报案及查勘带入）", False, MEDIA, readonly=True,
                  hint="首次办理带入查勘缺陷附件；已有修复记录时采用官网返回的缺陷附件，本环节不另传。"),
            files("recoveredFileInfo", "修复后照片/视频", True, MEDIA, suggest=["acceptance", "finished"]),
            files("supplementaryMaterialsFileInfo", "补充材料", False, [".pdf"], when={"repairMethod": ["2"]},
                  custom_type_code="supplementary_material", max_bytes_exclusive=20 * 1024 * 1024,
                  hint="计划维修可附 PDF；官网未设置独立施工过程照片栏，归入补充材料需按实际业务核对。"),
        ],
    },
    "acceptance": {
        "label": "维修成果验收", "guide_page": 14,
        "endpoint": "/aimsManage/repairAcceptance/acceptanceResult", "actions": ["submit"],
        "fields": [
            field("acceptanceDate", "验收时间", "datetime", True),
            files("acceptanceCheckFileInfo", "业主验收材料", True, [".pdf"],
                  custom_type_code="owner_cceptance_materials", max_bytes_exclusive=20 * 1024 * 1024,
                  hint="使用真实业主验收材料 PDF；完工照片不会自动转换或算作验收材料。"),
            files("supplementaryMaterialsFileInfo", "补充材料", False, [".pdf"],
                  custom_type_code="supplementary_material", max_bytes_exclusive=20 * 1024 * 1024),
        ],
    },
}

READ_ENDPOINTS = {
    "repair_list": "/aimsManage/reportReceiving/getThirdPartyRepairList",
    "intake_list": "/aimsManage/reportReceiving/getAttachmentSurveyList",
    "claim": "/aimsManage/reportReceiving/getInfo/",
    "survey": "/aimsManage/siteSurvey/getInfo/",
    "repair": "/aimsManage/repairAcceptance/getInfo/",
    "dictionary": "/insurance/sysUcenterDict/list",
    "personnel": "/aimsManage/maintenancePersonnel/getMaintenancePersonnelOrUnitPerson",
}


def visible(item, values):
    return all(str(values.get(key, "")) in allowed for key, allowed in item.get("when", {}).items())


def contract(dictionaries, write_enabled=False):
    return {"version": VERSION, "website": WEBSITE, "verified_on": "2026-10-15", "mode": "reviewed_writes" if write_enabled else "read_only",
            "stages": deepcopy(STAGES), "dictionaries": deepcopy(dictionaries),
            "notes": ["核对当前案件、全部字段与所选原件后，由用户确认提交官网。" if write_enabled else "官网只读，可保存本地草稿和核对记录。",
                      "楼栋、入户及缺陷照片按对应栏位上传，业务时间使用已核对的实际记录。",
                      "普通图片/视频栏未查到明确的网页大小上限；服务端限制和上传结果尚未实测。"]}
