package io.github.lolilandlinux;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.nio.charset.StandardCharsets;
import java.security.ProtectionDomain;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.List;

/** Java 17 compatible launcher fixes; never transforms game classes. */
public final class OsrAgent implements ClassFileTransformer {
    public static void premain(String options, Instrumentation instrumentation) {
        instrumentation.addTransformer(new OsrAgent());
    }

    @Override
    public byte[] transform(ClassLoader loader, String name, Class<?> type,
                            ProtectionDomain domain, byte[] bytes) {
        if (name == null || !name.startsWith("loliland/launcher/")) return null;
        try {
            if (name.equals("loliland/launcher/app/data/ClientLaunchConfig")) {
                byte[] result = fixUsernameQuoting(bytes);
                System.err.println("[LoliLand Linux] Linux username argument compatibility enabled");
                return result;
            }
            byte[] result = selectOffscreen(bytes);
            if (result != null) System.err.println("[LoliLand Linux] JCEF OSR: " + name);
            return result;
        } catch (Exception error) {
            // A format change must be visible, and must not damage the class.
            System.err.println("[LoliLand Linux] OSR compatibility failed: " + error);
            return null;
        }
    }

    private static byte[] fixUsernameQuoting(byte[] bytes) throws Exception {
        Pool pool = new Pool(bytes);
        Cursor cursor = new Cursor(bytes, pool.end);
        cursor.skip(2); // class access flags
        int thisClass = cursor.u2();
        cursor.skip(2); // superclass
        cursor.skip(cursor.u2() * 2); // interfaces
        int fields = cursor.u2();
        for (int i = 0; i < fields; i++) skipMember(cursor);
        int methodCountOffset = cursor.offset;
        int methods = cursor.u2();
        List<Member> members = new ArrayList<>();
        Member getter = null;
        String helperName = "lolilandLinux$originalGameArguments";
        for (int i = 0; i < methods; i++) {
            Member member = new Member(cursor, pool);
            if (helperName.equals(pool.strings[member.name])) return bytes;
            if ("getGameArguments".equals(pool.strings[member.name])
                    && "()Ljava/util/List;".equals(pool.strings[member.descriptor])) getter = member;
            members.add(member);
        }
        if (getter == null || (getter.access & (0x0008 | 0x0100 | 0x0400)) != 0)
            throw new IllegalArgumentException("Unsupported game arguments getter");
        if (methods == 65535) throw new IllegalArgumentException("Method table is full");
        int helper = pool.utf8(helperName);
        int helperType = pool.nameType(helper, getter.descriptor);
        int helperRef = pool.methodRef(thisClass, helperType);
        int agentClass = pool.classEntry(pool.utf8("io/github/lolilandlinux/OsrAgent"));
        int normalizeType = pool.nameType(pool.utf8("normalizeUsernamePlaceholder"),
                pool.utf8("(Ljava/util/List;)Ljava/util/List;"));
        int normalizeRef = pool.methodRef(agentClass, normalizeType);
        int codeName = pool.utf8("Code");
        ByteArrayOutputStream bodyBuffer = new ByteArrayOutputStream();
        DataOutputStream body = new DataOutputStream(bodyBuffer);
        body.write(bytes, pool.end, methodCountOffset - pool.end);
        body.writeShort(methods + 1);
        for (Member member : members) {
            if (member == getter) {
                // Keep the original bytecode and its stack maps untouched in a
                // private helper. A straight-line public wrapper normalizes its result.
                body.writeShort((member.access & ~0x0005) | 0x0002 | 0x1000);
                body.writeShort(helper);
                body.writeShort(member.descriptor);
                body.write(bytes, member.start + 6, member.end - member.start - 6);
            } else {
                body.write(bytes, member.start, member.end - member.start);
            }
        }
        body.writeShort(getter.access);
        body.writeShort(getter.name);
        body.writeShort(getter.descriptor);
        body.writeShort(getter.nonCode.size() + 1);
        for (byte[] attribute : getter.nonCode) body.write(attribute);
        body.writeShort(codeName);
        body.writeInt(20);
        body.writeShort(1); // max stack
        body.writeShort(1); // this
        body.writeInt(8);
        body.writeByte(0x2a); // aload_0
        body.writeByte(0xb7); body.writeShort(helperRef); // invokespecial original getter
        body.writeByte(0xb8); body.writeShort(normalizeRef); // invokestatic normalize
        body.writeByte(0xb0); // areturn
        body.writeShort(0); // exception table
        body.writeShort(0); // no branches: implicit empty stack-map table
        body.write(bytes, cursor.offset, bytes.length - cursor.offset);
        ByteArrayOutputStream result = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(result);
        output.write(bytes, 0, 8);
        output.writeShort(pool.count);
        output.write(bytes, 10, pool.end - 10);
        output.write(pool.extra.toByteArray());
        output.write(bodyBuffer.toByteArray());
        return result.toByteArray();
    }

    private static void skipMember(Cursor cursor) {
        cursor.skip(6);
        int attributes = cursor.u2();
        for (int i = 0; i < attributes; i++) {
            cursor.skip(2);
            cursor.skip(cursor.u4());
        }
    }

    private static final class Cursor {
        final byte[] bytes;
        int offset;
        Cursor(byte[] bytes, int offset) { this.bytes = bytes; this.offset = offset; }
        int u2() { int value = OsrAgent.u2(bytes, offset); skip(2); return value; }
        int u4() {
            int high = u2(), low = u2();
            if (high > 32767) throw new IllegalArgumentException("Oversized attribute");
            return (high << 16) | low;
        }
        void skip(int length) {
            if (length < 0 || offset > bytes.length - length)
                throw new IllegalArgumentException("Truncated class file");
            offset += length;
        }
    }

    private static final class Member {
        final int start, end, access, name, descriptor;
        final List<byte[]> nonCode = new ArrayList<>();
        Member(Cursor cursor, Pool pool) {
            start = cursor.offset;
            access = cursor.u2(); name = cursor.u2(); descriptor = cursor.u2();
            int attributes = cursor.u2();
            for (int i = 0; i < attributes; i++) {
                int begin = cursor.offset;
                int attributeName = cursor.u2();
                cursor.skip(cursor.u4());
                if (!"Code".equals(pool.strings[attributeName]))
                    nonCode.add(Arrays.copyOfRange(cursor.bytes, begin, cursor.offset));
            }
            end = cursor.offset;
        }
    }

    private static final class Pool {
        final int end;
        final String[] strings;
        int count;
        final ByteArrayOutputStream extra = new ByteArrayOutputStream();
        final DataOutputStream output = new DataOutputStream(extra);
        Pool(byte[] bytes) {
            Cursor cursor = new Cursor(bytes, 8);
            count = cursor.u2();
            strings = new String[count];
            for (int i = 1; i < count; i++) {
                int tag = bytes[cursor.offset++] & 255;
                switch (tag) {
                    case 1:
                        int size = cursor.u2();
                        strings[i] = new String(bytes, cursor.offset, size, StandardCharsets.UTF_8);
                        cursor.skip(size); break;
                    case 3: case 4: case 9: case 10: case 11: case 12: case 17: case 18:
                        cursor.skip(4); break;
                    case 5: case 6: cursor.skip(8); i++; break;
                    case 7: case 8: case 16: case 19: case 20: cursor.skip(2); break;
                    case 15: cursor.skip(3); break;
                    default: throw new IllegalArgumentException("Unknown constant tag " + tag);
                }
            }
            end = cursor.offset;
        }
        int entry() {
            if (count == 65535) throw new IllegalArgumentException("Constant pool is full");
            return count++;
        }
        int utf8(String text) throws Exception {
            int index = entry(); output.writeByte(1); output.writeUTF(text); return index;
        }
        int classEntry(int name) throws Exception {
            int index = entry(); output.writeByte(7); output.writeShort(name); return index;
        }
        int nameType(int name, int type) throws Exception {
            int index = entry(); output.writeByte(12); output.writeShort(name); output.writeShort(type); return index;
        }
        int methodRef(int owner, int type) throws Exception {
            int index = entry(); output.writeByte(10); output.writeShort(owner); output.writeShort(type); return index;
        }
    }

    /** Linux ProcessBuilder passes quote characters literally; change only the
     * username placeholder, before the official launcher substitutes the name.
     * All identity resolution, tokens and validation remain upstream behavior.
     */
    public static List<String> normalizeUsernamePlaceholder(List<String> arguments) {
        List<String> result = new ArrayList<>(arguments.size());
        for (String argument : arguments) {
            result.add(argument.replace("\"${auth_player_name}\"", "${auth_player_name}")
                    .replace("'${auth_player_name}'", "${auth_player_name}"));
        }
        return result;
    }

    private static int u2(byte[] bytes, int offset) {
        return ((bytes[offset] & 255) << 8) | (bytes[offset + 1] & 255);
    }

    /** Returns null when this class doesn't read CefRendering.DEFAULT. */
    static byte[] selectOffscreen(byte[] bytes) throws Exception {
        if (bytes.length < 10 || bytes[0] != (byte) 0xca || bytes[1] != (byte) 0xfe)
            throw new IllegalArgumentException("Invalid class file");
        int count = u2(bytes, 8);
        if (count > 65533) throw new IllegalArgumentException("Constant pool is full");
        int[] tags = new int[count];
        int[] first = new int[count];
        int[] second = new int[count];
        byte[][] entries = new byte[count][];
        String[] strings = new String[count];
        int offset = 10;
        for (int index = 1; index < count; index++) {
            int start = offset;
            int tag = bytes[offset++] & 255;
            tags[index] = tag;
            switch (tag) {
                case 1:
                    int length = u2(bytes, offset);
                    offset += 2;
                    strings[index] = new String(bytes, offset, length, StandardCharsets.UTF_8);
                    offset += length;
                    break;
                case 3: case 4: offset += 4; break;
                case 5: case 6: offset += 8; break;
                case 7: case 8: case 16: case 19: case 20:
                    first[index] = u2(bytes, offset); offset += 2; break;
                case 9: case 10: case 11: case 12: case 17: case 18:
                    first[index] = u2(bytes, offset);
                    second[index] = u2(bytes, offset + 2); offset += 4; break;
                case 15: offset += 3; break;
                default: throw new IllegalArgumentException("Unknown constant tag " + tag);
            }
            if (offset > bytes.length) throw new IllegalArgumentException("Truncated class file");
            entries[index] = Arrays.copyOfRange(bytes, start, offset);
            if (tag == 5 || tag == 6) index++;
        }
        boolean changed = false;
        int descriptor = 0;
        for (int index = 1; index < count; index++) {
            if (tags[index] != 9) continue;
            int owner = first[index];
            int member = second[index];
            if (tags[owner] != 7 || tags[member] != 12) continue;
            if (!"org/cef/browser/CefRendering".equals(strings[first[owner]])
                    || !"DEFAULT".equals(strings[first[member]])
                    || !"Lorg/cef/browser/CefRendering;".equals(strings[second[member]])) continue;
            descriptor = second[member];
            int replacement = count + 1;
            entries[index][3] = (byte) (replacement >>> 8);
            entries[index][4] = (byte) replacement;
            changed = true;
        }
        if (!changed) return null;
        ByteArrayOutputStream buffer = new ByteArrayOutputStream(bytes.length + 20);
        DataOutputStream output = new DataOutputStream(buffer);
        output.write(bytes, 0, 8);
        output.writeShort(count + 2);
        for (int index = 1; index < count; index++)
            if (entries[index] != null) output.write(entries[index]);
        output.writeByte(1); output.writeUTF("OFFSCREEN");
        output.writeByte(12); output.writeShort(count); output.writeShort(descriptor);
        output.write(bytes, offset, bytes.length - offset);
        return buffer.toByteArray();
    }
}
