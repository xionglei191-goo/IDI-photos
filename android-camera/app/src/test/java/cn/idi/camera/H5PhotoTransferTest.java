package cn.idi.camera;

import static org.junit.Assert.*;
import java.io.IOException;
import java.util.Base64;
import java.util.Random;
import org.junit.Test;

public class H5PhotoTransferTest {
  @Test public void preservesLargeBinaryAcrossOrderedChunks() throws Exception {
    byte[] original = new byte[176543];
    new Random(42).nextBytes(original);
    String encoded = Base64.getEncoder().encodeToString(original);
    H5PhotoTransfer transfer = new H5PhotoTransfer(encoded.length());
    for (int offset = 0, seq = 0; offset < encoded.length(); offset += H5PhotoTransfer.CHUNK_CHARS, seq++)
      transfer.append(seq, encoded.substring(offset, Math.min(encoded.length(), offset + H5PhotoTransfer.CHUNK_CHARS)));
    assertArrayEquals(original, transfer.finish());
    assertThrows(IOException.class, transfer::finish);
  }

  @Test public void rejectsIncompleteDuplicateMalformedAndOversizeInput() throws Exception {
    assertThrows(IOException.class, () -> new H5PhotoTransfer(0));
    assertThrows(IOException.class, () -> new H5PhotoTransfer(27962032));
    H5PhotoTransfer transfer = new H5PhotoTransfer(8);
    assertThrows(IOException.class, () -> transfer.append(1, "YWJj"));
    assertThrows(IOException.class, () -> transfer.append(0, "YWI="));
    assertThrows(IOException.class, () -> transfer.append(0, "!!!!"));
    transfer.append(0, "YWJj");
    assertThrows(IOException.class, () -> transfer.append(0, "YWJj"));
    assertThrows(IOException.class, transfer::finish);
    transfer.append(1, "ZA==");
    assertArrayEquals(new byte[]{'a','b','c','d'}, transfer.finish());
  }
}
