using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Http;
using System.Text;
using Microsoft.Xrm.Sdk;

namespace Timefold.FieldService
{
    /// <summary>
    /// The Timefold Model API, Field Service Routing model:
    ///   POST {base}/api/models/field-service-routing/v1/route-plans          -> job id
    ///   GET  {base}/api/models/field-service-routing/v1/route-plans/{id}/metadata
    ///   GET  {base}/api/models/field-service-routing/v1/route-plans/{id}
    /// Authentication is the X-API-KEY header.
    /// </summary>
    public class TimefoldClient : IDisposable
    {
        public static readonly HashSet<string> Done = new HashSet<string> { "SOLVING_COMPLETED", "SOLVING_INCOMPLETE" };
        public static readonly HashSet<string> Failed = new HashSet<string> { "SOLVING_FAILED", "DATASET_INVALID" };
        private const string Path = "/api/models/field-service-routing/v1/route-plans";
        private readonly HttpClient http;

        public TimefoldClient(string apiKey, string baseUrl)
        {
            if (string.IsNullOrWhiteSpace(apiKey))
                throw new InvalidPluginExecutionException(
                    "No Timefold API key. Get one free at https://app.timefold.ai (Settings > API keys) and put it in " +
                    "the 'Timefold API key' environment variable (tf_TimefoldApiKey), or tick Dry run.");
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            http = new HttpClient { BaseAddress = new Uri(baseUrl.TrimEnd('/')), Timeout = TimeSpan.FromSeconds(45) };
            http.DefaultRequestHeaders.Add("X-API-KEY", apiKey.Trim());
            http.DefaultRequestHeaders.Add("Accept", "application/json");
        }

        private string Send(HttpMethod method, string path, string body)
        {
            for (var attempt = 1; ; attempt++)
            {
                var request = new HttpRequestMessage(method, path);
                if (body != null) request.Content = new StringContent(body, Encoding.UTF8, "application/json");
                HttpResponseMessage response;
                try { response = http.SendAsync(request).GetAwaiter().GetResult(); }
                catch (HttpRequestException) when (attempt < 3 && method == HttpMethod.Get) { System.Threading.Thread.Sleep(1000 * attempt); continue; }
                var text = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                var code = (int)response.StatusCode;
                if ((code == 429 || code >= 502) && attempt < 3) { System.Threading.Thread.Sleep(1000 * attempt); continue; }
                if (code == 401)
                    throw new InvalidPluginExecutionException("Timefold refused the API key (401). Check tf_TimefoldApiKey.");
                if (code == 403)
                {
                    // The key is valid but not allowed this: usually the tenant has no
                    // subscription to the model (TFP-11001). Timefold's message says which.
                    var reason = Json.Str(Json.Obj(SafeParse(text)), "message") ?? text;
                    throw new InvalidPluginExecutionException("Timefold refused the request (403): " + reason +
                        ". Check that your Timefold tenant has the Field Service Routing model enabled.");
                }
                if (code >= 400)
                    throw new InvalidPluginExecutionException("Timefold returned " + code + " for " + method + " " + path + ": " +
                                                              (text.Length > 1500 ? text.Substring(0, 1500) : text));
                return text;
            }
        }

        private static object SafeParse(string text)
        {
            try { return Json.Parse(text); } catch (FormatException) { return null; }
        }

        public string Submit(string requestJson)
        {
            var text = Send(HttpMethod.Post, Path, requestJson);
            var parsed = Json.Parse(text);
            var id = parsed as string ?? Json.Str(Json.Obj(parsed), "id");
            if (string.IsNullOrEmpty(id)) throw new InvalidPluginExecutionException("Timefold returned no job id: " + text);
            return id;
        }

        public Dictionary<string, object> Metadata(string jobId)
        {
            return Json.Obj(Json.Parse(Send(HttpMethod.Get, Path + "/" + jobId + "/metadata", null)));
        }

        public Dictionary<string, object> Result(string jobId)
        {
            return Json.Obj(Json.Parse(Send(HttpMethod.Get, Path + "/" + jobId, null)));
        }

        public Dictionary<string, object> Validation(string jobId)
        {
            try { return Json.Obj(Json.Parse(Send(HttpMethod.Get, Path + "/" + jobId + "/validation-result", null))); }
            catch (Exception) { return null; }               // never fail a run over this
        }

        public void Dispose()
        {
            http.Dispose();
        }
    }
}
