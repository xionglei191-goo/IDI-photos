package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.content.Context;
import android.app.UiAutomation;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.EditText;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Synthetic UI fixtures only; opt in on an isolated emulator, never on a paired field phone. */
@RunWith(AndroidJUnit4.class)
public class CaseFilterDeviceTest {
  private Context context;
  private CaptureStore store;
  private Map<String, ?> originalPreferences;
  private JSONArray originalCases;
  private JSONArray fixture;
  private String fixtureDevice;

  @Before
  public void prepareIsolatedCases() throws Exception {
    assumeTrue("Run the debug APK with -e case_filter_fixture 1",
        BuildConfig.DEBUG && "1".equals(
            InstrumentationRegistry.getArguments().getString("case_filter_fixture", "")));
    assumeTrue("Case filters use synthetic fixtures on an emulator only",
        Build.PRODUCT.startsWith("sdk") || Build.FINGERPRINT.contains("generic"));
    // Connect before launching the first window: the initial accessibility-service
    // handshake must not consume the dialog polling deadline on a cold test run.
    InstrumentationRegistry.getInstrumentation().getUiAutomation();
    context = ApplicationProvider.getApplicationContext();
    store = CaptureStore.get(context);
    CaptureStore.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    for (CaptureStore.Item item : store.items()) {
      assumeTrue("Finish or isolate existing capture work before this UI-only fixture",
          !store.activeCaptures.contains(item.id)
              && !Arrays.asList("capturing", "rendering", "awaiting_sdk", "queued", "retry",
                  "uploading", "verifying").contains(item.state)
              && !(item.official()
                  && Arrays.asList("capture_failed", "cancelled").contains(item.state)
                  && store.original(item.id).isFile()));
    }
    originalPreferences = new HashMap<>(store.prefs.getAll());
    originalCases = new JSONArray();
    for (JSONObject item : store.cases()) originalCases.put(new JSONObject(item.toString()));
    fixtureDevice = "case-filter-fixture-" + UUID.randomUUID();
    assertTrue(store.prefs.edit().clear()
        .putString("server", "http://127.0.0.1:1")
        .putString("device", fixtureDevice)
        .putBoolean("demo", false)
        .putBoolean("upload_notification_asked", true)
        .commit());
    fixture = fixtures();
    store.replaceCases(fixture);
    assertFalse("No SDK authorization or server request is needed", store.connected());
  }

  @After
  public void restoreExistingCacheAndPreferences() throws Exception {
    if (originalPreferences == null) return;
    CaptureStore.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    store.replaceCases(originalCases);
    SharedPreferences.Editor editor = store.prefs.edit().clear();
    for (Map.Entry<String, ?> entry : originalPreferences.entrySet()) {
      String key = entry.getKey();
      Object value = entry.getValue();
      if (value instanceof String) editor.putString(key, (String) value);
      else if (value instanceof Boolean) editor.putBoolean(key, (Boolean) value);
      else if (value instanceof Integer) editor.putInt(key, (Integer) value);
      else if (value instanceof Long) editor.putLong(key, (Long) value);
      else if (value instanceof Float) editor.putFloat(key, (Float) value);
      else if (value instanceof Set<?>) {
        Set<String> strings = new HashSet<>();
        for (Object element : (Set<?>) value) strings.add((String) element);
        editor.putStringSet(key, strings);
      } else fail("Unexpected preference value type for " + key);
    }
    assertTrue(editor.commit());
  }

  @Test
  public void stagesAndProjectAndSearchCombineWithoutHidingOtherStagesFromAll() throws Exception {
    try (ActivityScenario<MainActivity> scenario = launch()) {
      assertCases(scenario, "101", "201", "202", "301", "401", "501", "601");
      shot("case-filter-default.png");
      openFilter(scenario, "状态");
      shot("case-filter-stage-options.png");
      selectDialogOption("状态", "查勘");
      assertCases(scenario, "101");
      choose(scenario, "状态", "施工");
      assertCases(scenario, "201", "202", "501");
      choose(scenario, "状态", "验收");
      assertCases(scenario, "301");
      choose(scenario, "状态", "全部状态");
      // Settlement and an unfamiliar future stage remain discoverable in the unfiltered list.
      assertCases(scenario, "101", "201", "202", "301", "401", "501", "601");
      choose(scenario, "项目", "都会A区");
      choose(scenario, "状态", "施工");
      assertCases(scenario, "201", "202");
      search(scenario, "  syn-repair-201  ");
      assertCases(scenario, "201");
      assertFilters(scenario, "都会A区", "施工", "  syn-repair-201  ");
      shot("case-filter-project-stage.png");
      choose(scenario, "项目", "都会B区");
      assertCases(scenario);
      assertText(scenario, "没有找到匹配的案件");
      search(scenario, "");
      assertCases(scenario, "501");
    }
  }

  @Test
  public void filtersSurviveCameraBackRecordsRecreationAndRefreshedCaseData() throws Exception {
    try (ActivityScenario<MainActivity> scenario = launch()) {
      choose(scenario, "项目", "都会A区");
      choose(scenario, "状态", "施工");
      search(scenario, "  SYN-REPAIR-201  ");
      JSONObject selected = fixture.getJSONObject(1);
      scenario.onActivity(a -> {
        a.select(selected);
        assertEquals("camera", a.screen);
        assertNotNull(a.officialScreen);
        assertFalse(a.store.connected());
        a.getOnBackPressedDispatcher().onBackPressed();
      });
      idle();
      assertFilters(scenario, "都会A区", "施工", "  SYN-REPAIR-201  ");
      assertCases(scenario, "201");
      scenario.onActivity(a -> a.show("records"));
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "都会A区", "施工", "  SYN-REPAIR-201  ");
      scenario.recreate();
      idle();
      assertFilters(scenario, "都会A区", "施工", "  SYN-REPAIR-201  ");
      assertCases(scenario, "201");

      // Sync can change a case's stage; keep the user's filters while updating the result set.
      JSONArray changed = new JSONArray(fixture.toString());
      changed.getJSONObject(1).put("workflow_stage", "acceptance").put("stage_label", "待验收");
      store.replaceCases(changed);
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "都会A区", "施工", "  SYN-REPAIR-201  ");
      assertCases(scenario);
      assertText(scenario, "没有找到匹配的案件");
      store.replaceCases(fixture);
      scenario.onActivity(a -> a.show("cases"));
      assertCases(scenario, "201");
      assertFilters(scenario, "都会A区", "施工", "  SYN-REPAIR-201  ");
      shot("case-filter-returned.png");
    }
  }

  @Test
  public void demoAndDifferentPairingsDoNotReuseEachOthersFilters() throws Exception {
    try (ActivityScenario<MainActivity> scenario = launch()) {
      choose(scenario, "项目", "都会A区");
      choose(scenario, "状态", "施工");
      search(scenario, "201");

      assertTrue(store.prefs.edit().putString("device", fixtureDevice + "-other").commit());
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "全部项目", "全部状态", "");
      choose(scenario, "项目", "都会B区");
      assertTrue(store.prefs.edit().putString("device", fixtureDevice).commit());
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "都会A区", "施工", "201");

      assertTrue(store.prefs.edit().putString("server", "http://127.0.0.1:2").commit());
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "全部项目", "全部状态", "");
      assertTrue(store.prefs.edit().putString("server", "http://127.0.0.1:1").commit());
      scenario.onActivity(a -> a.show("cases"));
      assertFilters(scenario, "都会A区", "施工", "201");

      scenario.onActivity(MainActivity::startDemo);
      assertFilters(scenario, "全部项目", "全部状态", "");
      choose(scenario, "项目", "铂悦府（示例）");
      choose(scenario, "状态", "验收");
      search(scenario, "508");
      scenario.onActivity(a -> {
        a.demo = false;
        a.store.prefs.edit().putBoolean("demo", false).apply();
        a.show("cases");
      });
      assertFilters(scenario, "都会A区", "施工", "201");
      scenario.onActivity(MainActivity::startDemo);
      assertFilters(scenario, "铂悦府（示例）", "验收", "508");
    }
  }

  @Test
  public void longProjectNameKeepsBothFilterControlsVisibleAndTouchable() throws Exception {
    String longName = "都会A区 · 很长的小区项目名称用于检查筛选栏";
    JSONArray longNames = new JSONArray(fixture.toString());
    for (int i = 0; i < longNames.length(); i++) {
      JSONObject item = longNames.getJSONObject(i);
      if (item.getString("project_id").equals("fixture-a")) item.put("project_name", longName);
    }
    store.replaceCases(longNames);
    try (ActivityScenario<MainActivity> scenario = launch()) {
      choose(scenario, "项目", longName);
      choose(scenario, "状态", "施工");
      awaitFilterLayout(scenario, "long project after selection");
      scenario.onActivity(a -> {
        View project = findDescription(a.root, "项目筛选：" + longName);
        View stage = findDescription(a.root, "状态筛选：施工");
        assertNotNull(project);
        assertNotNull(stage);
        Rect projectRect = new Rect(), stageRect = new Rect(), pageRect = new Rect();
        assertTrue("Project control must be visible: " + viewState(project),
            project.getGlobalVisibleRect(projectRect));
        assertTrue("Stage control must be visible: " + viewState(stage),
            stage.getGlobalVisibleRect(stageRect));
        assertTrue("Activity content must be visible", a.root.getGlobalVisibleRect(pageRect));
        String bounds = "project=" + projectRect + ", stage=" + stageRect + ", page=" + pageRect;
        assertTrue("Project touch height: " + bounds, projectRect.height() >= a.ui.dp(48));
        assertTrue("Project touch width: " + bounds, projectRect.width() >= a.ui.dp(48));
        assertTrue("Stage touch height: " + bounds, stageRect.height() >= a.ui.dp(48));
        assertTrue("Stage touch width: " + bounds, stageRect.width() >= a.ui.dp(48));
        assertEquals("Filter controls must share one row: " + bounds, projectRect.top, stageRect.top);
        assertTrue("Filter controls must not overlap: " + bounds,
            projectRect.right + a.ui.dp(8) <= stageRect.left);
        assertTrue("Project must fit inside the page: " + bounds, pageRect.contains(projectRect));
        assertTrue("Stage must fit inside the page: " + bounds, pageRect.contains(stageRect));
      });
      shot("case-filter-long-project.png");
    }
  }

  private ActivityScenario<MainActivity> launch() {
    ActivityScenario<MainActivity> scenario = ActivityScenario.launch(
        new Intent(context, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
    try {
      awaitFilterLayout(scenario, "initial activity window");
      return scenario;
    } catch (RuntimeException | Error error) {
      scenario.close();
      throw error;
    }
  }

  private static JSONArray fixtures() throws Exception {
    return new JSONArray()
        .put(item("101", "fixture-a", "都会A区", "survey", "查勘中", "SYN-SURVEY-101"))
        .put(item("201", "fixture-a", "都会A区", "repair", "施工中", "SYN-REPAIR-201"))
        .put(item("202", "fixture-a", "都会A区", "repair", "施工中", "SYN-REPAIR-202"))
        .put(item("301", "fixture-a", "都会A区", "acceptance", "待验收", "SYN-ACCEPT-301"))
        .put(item("401", "fixture-a", "都会A区", "settlement", "待结算", "SYN-SETTLE-401"))
        .put(item("501", "fixture-b", "都会B区", "repair", "施工中", "SYN-REPAIR-501"))
        .put(item("601", "fixture-a", "都会A区", "future_stage", "其他阶段", "SYN-OTHER-601"));
  }

  private static JSONObject item(String room, String project, String projectName, String stage,
      String stageLabel, String number) throws Exception {
    return new JSONObject().put("id", "case-filter-" + room).put("project_id", project)
        .put("project_name", projectName).put("workflow_stage", stage).put("stage_label", stageLabel)
        .put("location", "测试楼栋 · " + room + "室").put("title", "筛选测试维修内容")
        .put("case_number", number).put("parts", new JSONArray().put("卫生间"))
        .put("capture_enabled", true);
  }

  private static void choose(ActivityScenario<MainActivity> scenario, String kind, String label) {
    openFilter(scenario, kind);
    selectDialogOption(kind, label);
    awaitFilterLayout(scenario, "after selecting " + kind + ": " + label);
    scenario.onActivity(a -> assertNotNull("Selected filter was not applied: " + kind + ": " + label,
        findDescription(a.root, kind + "筛选：" + label)));
  }

  private static void openFilter(ActivityScenario<MainActivity> scenario, String kind) {
    awaitFilterLayout(scenario, "before opening " + kind + " dialog");
    scenario.onActivity(a -> {
      View control = findDescriptionPrefix(a.root, kind + "筛选：");
      assertNotNull(kind + " filter control", control);
      assertTrue(control.performClick());
    });
    idle();
  }

  private static void selectDialogOption(String kind, String label) {
    UiAutomation automation = InstrumentationRegistry.getInstrumentation().getUiAutomation();
    long deadline = SystemClock.elapsedRealtime() + 5000;
    String observed = "no accessibility root";
    while (SystemClock.elapsedRealtime() < deadline) {
      AccessibilityNodeInfo root = automation.getRootInActiveWindow();
      if (root != null) {
        List<AccessibilityNodeInfo> matches = root.findAccessibilityNodeInfosByText(label);
        boolean dialogVisible = !root.findAccessibilityNodeInfosByText("选择" + kind).isEmpty();
        observed = "package=" + root.getPackageName() + ", window=" + root.getWindowId()
            + ", dialogTitle=" + dialogVisible + ", matchingNodes=" + matches.size();
        for (AccessibilityNodeInfo node : matches) {
          if (!dialogVisible || !node.isVisibleToUser()) continue;
          if (!label.contentEquals(node.getText() == null ? "" : node.getText())) continue;
          AccessibilityNodeInfo clickable = node;
          while (clickable != null && !clickable.isClickable()) clickable = clickable.getParent();
          if (clickable != null && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            idle();
            return;
          }
        }
      }
      SystemClock.sleep(50);
    }
    fail("Could not select " + label + " from " + kind + " dialog; " + observed);
  }

  private static void search(ActivityScenario<MainActivity> scenario, String text) {
    awaitFilterLayout(scenario, "before setting search");
    scenario.onActivity(a -> {
      EditText search = findSearch(a.root);
      assertNotNull(search);
      search.setText(text);
    });
    awaitFilterLayout(scenario, "after setting search");
  }

  private static void assertFilters(ActivityScenario<MainActivity> scenario, String project,
      String stage, String query) {
    awaitFilterLayout(scenario, "checking restored filters");
    scenario.onActivity(a -> {
      assertEquals("cases", a.screen);
      assertNotNull(findDescription(a.root, "项目筛选：" + project));
      assertNotNull(findDescription(a.root, "状态筛选：" + stage));
      EditText search = findSearch(a.root);
      assertNotNull(search);
      assertEquals(query, search.getText().toString());
    });
  }

  private static void assertCases(ActivityScenario<MainActivity> scenario, String... rooms) {
    awaitFilterLayout(scenario, "checking filtered cases");
    scenario.onActivity(a -> {
      Set<String> actual = new HashSet<>();
      for (String text : texts(a.root))
        if (text.startsWith("测试楼栋 · ") && text.endsWith("室"))
          actual.add(text.substring("测试楼栋 · ".length(), text.length() - 1));
      assertEquals(new HashSet<>(Arrays.asList(rooms)), actual);
    });
  }

  private static void assertText(ActivityScenario<MainActivity> scenario, String text) {
    scenario.onActivity(a -> assertTrue("Missing text: " + text, texts(a.root).contains(text)));
  }

  private static List<String> texts(View view) {
    List<String> values = new ArrayList<>();
    if (view instanceof TextView) values.add(((TextView) view).getText().toString());
    if (view instanceof ViewGroup) {
      ViewGroup group = (ViewGroup) view;
      for (int i = 0; i < group.getChildCount(); i++) values.addAll(texts(group.getChildAt(i)));
    }
    return values;
  }

  private static View findDescription(View view, String description) {
    if (description.contentEquals(view.getContentDescription() == null ? "" : view.getContentDescription()))
      return view;
    if (view instanceof ViewGroup) {
      ViewGroup group = (ViewGroup) view;
      for (int i = 0; i < group.getChildCount(); i++) {
        View found = findDescription(group.getChildAt(i), description);
        if (found != null) return found;
      }
    }
    return null;
  }

  private static View findDescriptionPrefix(View view, String prefix) {
    if (view.getContentDescription() != null && view.getContentDescription().toString().startsWith(prefix))
      return view;
    if (view instanceof ViewGroup) {
      ViewGroup group = (ViewGroup) view;
      for (int i = 0; i < group.getChildCount(); i++) {
        View found = findDescriptionPrefix(group.getChildAt(i), prefix);
        if (found != null) return found;
      }
    }
    return null;
  }

  private static EditText findSearch(View view) {
    if (view instanceof EditText
        && "搜索楼栋、房号、案件号".contentEquals(((EditText) view).getHint())) return (EditText) view;
    if (view instanceof ViewGroup) {
      ViewGroup group = (ViewGroup) view;
      for (int i = 0; i < group.getChildCount(); i++) {
        EditText found = findSearch(group.getChildAt(i));
        if (found != null) return found;
      }
    }
    return null;
  }

  private static void idle() {
    InstrumentationRegistry.getInstrumentation().waitForIdleSync();
  }

  private static void awaitFilterLayout(ActivityScenario<MainActivity> scenario, String reason) {
    long deadline = SystemClock.elapsedRealtime() + 5000;
    AtomicBoolean ready = new AtomicBoolean();
    String[] observed = {"activity callback not reached"};
    while (SystemClock.elapsedRealtime() < deadline) {
      scenario.onActivity(a -> {
        View root = a.root;
        View project = root == null ? null : findDescriptionPrefix(root, "项目筛选：");
        View stage = root == null ? null : findDescriptionPrefix(root, "状态筛选：");
        Rect projectBounds = new Rect(), stageBounds = new Rect();
        boolean projectVisible = project != null && project.getGlobalVisibleRect(projectBounds);
        boolean stageVisible = stage != null && stage.getGlobalVisibleRect(stageBounds);
        ready.set(root != null && root.hasWindowFocus() && root.isAttachedToWindow()
            && root.isLaidOut() && !root.isLayoutRequested()
            && projectVisible && stageVisible && project.isLaidOut() && stage.isLaidOut()
            && !project.isLayoutRequested() && !stage.isLayoutRequested());
        observed[0] = "screen=" + a.screen + ", root=" + viewState(root)
            + ", project=" + viewState(project) + ", projectVisible=" + projectVisible
            + ", stage=" + viewState(stage) + ", stageVisible=" + stageVisible;
      });
      if (ready.get()) return;
      SystemClock.sleep(30);
    }
    fail("Filter layout did not become ready (" + reason + "): " + observed[0]);
  }

  private static String viewState(View view) {
    return view == null ? "null" : view.getWidth() + "x" + view.getHeight()
        + " shown=" + view.isShown() + " attached=" + view.isAttachedToWindow()
        + " focus=" + view.hasWindowFocus() + " layoutRequested=" + view.isLayoutRequested();
  }

  private void shot(String name) throws Exception {
    idle();
    SystemClock.sleep(200); // Let the platform dialog/window enter transition finish.
    idle();
    File folder = new File(context.getFilesDir(), "verification/case-filters");
    assertTrue(folder.isDirectory() || folder.mkdirs());
    Bitmap image = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (FileOutputStream output = new FileOutputStream(new File(folder, name))) {
      assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, output));
    } finally {
      image.recycle();
    }
  }
}
