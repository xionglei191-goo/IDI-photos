package cn.idi.camera;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

final class CameraData {
  static final String[] GROUPS = {
    "survey_checkin", "survey_problem", "work_checkin", "construction", "completion"
  };
  static final String[] LABELS = {"查勘打卡", "问题照片", "施工打卡", "施工过程", "完工照片"};
  static final String[] SHORT_LABELS = {"查勘打卡", "问题", "施工打卡", "施工", "完工"};

  static String label(String group) {
    for (int i = 0; i < GROUPS.length; i++) if (GROUPS[i].equals(group)) return LABELS[i];
    return "待分类";
  }

  static String role(String group, String checkinRole) {
    if (group.endsWith("checkin")) return checkinRole;
    return switch (group) {
      case "survey_problem" -> "problem";
      case "completion" -> "finished";
      default -> "construction";
    };
  }

  static JSONObject metadata(
      JSONObject selected, String group, String part, String content, String checkinRole)
      throws JSONException {
    return new JSONObject()
        .put("schema_version", 1)
        .put("capture_id", UUID.randomUUID().toString())
        .put("case_id", selected.getString("id"))
        .put("case_number", selected.getString("case_number"))
        .put("project_name", selected.getString("project_name"))
        .put("location", selected.getString("location"))
        .put(
            "captured_at",
            OffsetDateTime.now(ZoneOffset.ofHours(8))
                .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME))
        .put("photo_group", group)
        .put("role", role(group, checkinRole))
        .put("part", part)
        .put("content", content.trim())
        .put("app_version", BuildConfig.VERSION_NAME);
  }

  static String[] watermark(JSONObject meta) {
    OffsetDateTime time = OffsetDateTime.parse(meta.optString("captured_at"));
    String stage = label(meta.optString("photo_group"));
    if (meta.optString("photo_group").endsWith("checkin"))
      stage += " · " + ("building_checkin".equals(meta.optString("role")) ? "楼栋" : "入户门");
    return new String[] {
      time.format(DateTimeFormatter.ofPattern("HH:mm | yyyy-MM-dd")),
      "案件号：" + meta.optString("case_number"),
      "小区：" + meta.optString("project_name"),
      "位置：" + meta.optString("location"),
      "环节：" + stage,
      "内容：" + meta.optString("content")
    };
  }

  static List<JSONObject> objects(JSONArray array) {
    List<JSONObject> result = new ArrayList<>();
    for (int i = 0; i < array.length(); i++) {
      JSONObject item = array.optJSONObject(i);
      if (item != null) result.add(item);
    }
    return result;
  }

  static String date(JSONObject meta) {
    return meta.optString("captured_at").substring(0, 10);
  }

  static String time(JSONObject meta) {
    return OffsetDateTime.parse(meta.optString("captured_at"))
        .format(DateTimeFormatter.ofPattern("HH:mm:ss"));
  }

  static String recordedTime(CaptureStore.Item item) {
    if (item.verified() && item.state.equals("uploaded")) {
      try {
        String official =
            new JSONObject(item.receipt).getJSONObject("verification").getString("photoTime");
        return java.time.LocalDateTime.parse(
                    official, DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))
                .format(DateTimeFormatter.ofPattern("HH:mm:ss"))
            + " · 官方时间";
      } catch (Exception ignored) {
        return "官方时间待核对";
      }
    }
    String sdkTime = item.meta.optString("sdk_photo_time");
    try {
      if (item.official() && !sdkTime.isEmpty())
        return java.time.LocalDateTime.parse(sdkTime, DateTimeFormatter.ofPattern("yyyy:MM:dd HH:mm:ss"))
            .format(DateTimeFormatter.ofPattern("HH:mm:ss")) + " · SDK 时间（未验真）";
    } catch (Exception ignored) {
    }
    return time(item.meta) + (item.official() ? " · 手机记录" : "");
  }

  static String recordedDate(CaptureStore.Item item) {
    if (item.verified() && item.state.equals("uploaded")) {
      try {
        return new JSONObject(item.receipt)
            .getJSONObject("verification")
            .getString("photoTime")
            .substring(0, 10);
      } catch (Exception ignored) {
      }
    }
    try {
      String sdkTime = item.meta.optString("sdk_photo_time");
      if (item.official() && !sdkTime.isEmpty())
        return java.time.LocalDateTime.parse(sdkTime, DateTimeFormatter.ofPattern("yyyy:MM:dd HH:mm:ss"))
            .toLocalDate().toString();
    } catch (Exception ignored) {
    }
    return date(item.meta);
  }
}
