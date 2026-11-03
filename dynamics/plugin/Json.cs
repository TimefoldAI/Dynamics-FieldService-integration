using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Timefold.FieldService
{
    /// <summary>
    /// A small JSON reader and writer. Dataverse runs plug-ins in a sandbox that
    /// cannot load third-party assemblies, so there is no Newtonsoft here.
    /// Objects are Dictionary&lt;string, object&gt; (insertion ordered by the
    /// writer's caller), arrays List&lt;object&gt;, numbers double or long.
    /// </summary>
    public static class Json
    {
        // ------------------------------------------------------------- write
        public static string Write(object value)
        {
            var sb = new StringBuilder();
            Write(sb, value);
            return sb.ToString();
        }

        private static void Write(StringBuilder sb, object value)
        {
            if (value == null) { sb.Append("null"); return; }
            var s = value as string;
            if (s != null) { WriteString(sb, s); return; }
            if (value is bool) { sb.Append((bool)value ? "true" : "false"); return; }
            if (value is int || value is long) { sb.Append(Convert.ToInt64(value).ToString(CultureInfo.InvariantCulture)); return; }
            if (value is double || value is float || value is decimal)
            {
                sb.Append(Convert.ToDouble(value).ToString("R", CultureInfo.InvariantCulture)); return;
            }
            var dict = value as IDictionary<string, object>;
            if (dict != null)
            {
                sb.Append('{');
                var first = true;
                foreach (var kv in dict)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    WriteString(sb, kv.Key);
                    sb.Append(':');
                    Write(sb, kv.Value);
                }
                sb.Append('}');
                return;
            }
            var list = value as System.Collections.IEnumerable;
            if (list != null)
            {
                sb.Append('[');
                var first = true;
                foreach (var item in list)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    Write(sb, item);
                }
                sb.Append(']');
                return;
            }
            WriteString(sb, value.ToString());
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        // -------------------------------------------------------------- read
        public static object Parse(string text)
        {
            var i = 0;
            var value = ReadValue(text, ref i);
            SkipWhite(text, ref i);
            if (i != text.Length) throw new FormatException("Trailing characters in JSON at " + i);
            return value;
        }

        private static void SkipWhite(string t, ref int i)
        {
            while (i < t.Length && char.IsWhiteSpace(t[i])) i++;
        }

        private static object ReadValue(string t, ref int i)
        {
            SkipWhite(t, ref i);
            if (i >= t.Length) throw new FormatException("Unexpected end of JSON");
            var c = t[i];
            if (c == '{')
            {
                var obj = new Dictionary<string, object>();
                i++;
                SkipWhite(t, ref i);
                if (t[i] == '}') { i++; return obj; }
                while (true)
                {
                    SkipWhite(t, ref i);
                    var key = ReadString(t, ref i);
                    SkipWhite(t, ref i);
                    Expect(t, ref i, ':');
                    obj[key] = ReadValue(t, ref i);
                    SkipWhite(t, ref i);
                    if (t[i] == ',') { i++; continue; }
                    Expect(t, ref i, '}');
                    return obj;
                }
            }
            if (c == '[')
            {
                var list = new List<object>();
                i++;
                SkipWhite(t, ref i);
                if (t[i] == ']') { i++; return list; }
                while (true)
                {
                    list.Add(ReadValue(t, ref i));
                    SkipWhite(t, ref i);
                    if (t[i] == ',') { i++; continue; }
                    Expect(t, ref i, ']');
                    return list;
                }
            }
            if (c == '"') return ReadString(t, ref i);
            if (t.Substring(i).StartsWith("true")) { i += 4; return true; }
            if (t.Substring(i).StartsWith("false")) { i += 5; return false; }
            if (t.Substring(i).StartsWith("null")) { i += 4; return null; }
            var start = i;
            while (i < t.Length && "+-0123456789.eE".IndexOf(t[i]) >= 0) i++;
            var number = t.Substring(start, i - start);
            if (number.Length == 0) throw new FormatException("Unexpected '" + c + "' in JSON at " + start);
            long whole;
            if (number.IndexOfAny(new[] { '.', 'e', 'E' }) < 0 &&
                long.TryParse(number, NumberStyles.Integer, CultureInfo.InvariantCulture, out whole))
                return whole;
            return double.Parse(number, NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        private static void Expect(string t, ref int i, char c)
        {
            if (i >= t.Length || t[i] != c) throw new FormatException("Expected '" + c + "' in JSON at " + i);
            i++;
        }

        private static string ReadString(string t, ref int i)
        {
            Expect(t, ref i, '"');
            var sb = new StringBuilder();
            while (t[i] != '"')
            {
                var c = t[i++];
                if (c != '\\') { sb.Append(c); continue; }
                var e = t[i++];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u':
                        sb.Append((char)Convert.ToInt32(t.Substring(i, 4), 16));
                        i += 4;
                        break;
                    default: sb.Append(e); break;
                }
            }
            i++;
            return sb.ToString();
        }

        // ----------------------------------------------------------- helpers
        public static Dictionary<string, object> Obj(object value)
        {
            return value as Dictionary<string, object> ?? new Dictionary<string, object>();
        }

        public static List<object> Arr(object value)
        {
            return value as List<object> ?? new List<object>();
        }

        public static object Get(Dictionary<string, object> obj, string key)
        {
            object value;
            return obj != null && obj.TryGetValue(key, out value) ? value : null;
        }

        public static string Str(Dictionary<string, object> obj, string key)
        {
            var v = Get(obj, key);
            return v == null ? null : Convert.ToString(v, CultureInfo.InvariantCulture);
        }

        public static long Long(Dictionary<string, object> obj, string key, long fallback)
        {
            var v = Get(obj, key);
            return v == null ? fallback : Convert.ToInt64(v, CultureInfo.InvariantCulture);
        }

        public static bool Bool(Dictionary<string, object> obj, string key, bool fallback)
        {
            var v = Get(obj, key);
            return v is bool ? (bool)v : fallback;
        }
    }
}
