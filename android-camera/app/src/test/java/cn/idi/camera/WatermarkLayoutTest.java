package cn.idi.camera;

import static org.junit.Assert.*;

import org.junit.Test;

public class WatermarkLayoutTest {
  @Test
  public void keepsSmallAreaForPortraitLandscapeAndLongIdentifiers() {
    for (int[] size :
        new int[][] {{1080, 1440}, {4032, 3024}, {3024, 4032}, {1080, 1920}, {1920, 1080}}) {
      for (float em : new float[] {20f, 28f, 44f}) {
        for (int lines : new int[] {6, 8, 12}) {
          WatermarkLayout layout = new WatermarkLayout(size[0], size[1], em, lines);
          assertTrue(layout.fraction > 0 && layout.fraction <= .061f);
          assertTrue(layout.x > 0 && layout.y > 0);
          assertTrue(layout.x + layout.width < size[0]);
          assertTrue(layout.y + layout.height < size[1]);
          assertTrue(layout.width <= size[0] * .481f);
          assertTrue(layout.height <= size[1] * .251f);
        }
      }
    }
  }

  @Test
  public void scalingPhotoKeepsIdenticalProportions() {
    WatermarkLayout one = new WatermarkLayout(1080, 1440, 24, 6);
    WatermarkLayout three = new WatermarkLayout(3240, 4320, 24, 6);
    assertEquals(one.fraction, three.fraction, .00001);
    assertEquals(one.font * 3, three.font, .001);
  }

  @Test
  public void onlyPrivateHostsAllowHttp() throws Exception {
    assertEquals("https://example.com", CameraApi.normalizeServer("https://example.com/"));
    assertEquals(
        "http://192.168.31.252:8766", CameraApi.normalizeServer("http://192.168.31.252:8766"));
    for (String invalid :
        new String[] {
          "http://example.com",
          "http://192.168.3.999",
          "https://user:pass@example.com",
          "https://example.com/path",
          "javascript:alert(1)"
        }) {
      try {
        CameraApi.normalizeServer(invalid);
        fail(invalid);
      } catch (java.io.IOException expected) {
      }
    }
  }
}
