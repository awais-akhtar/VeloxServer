use std::ffi::{CStr, CString};
use std::fs;
use std::os::raw::c_char;
use std::path::{Component, Path, PathBuf};

#[repr(C)]
pub struct VeloxCoreVersion {
    pub major: u16,
    pub minor: u16,
    pub patch: u16,
}

#[repr(C)]
pub struct VeloxBuffer {
    pub ptr: *mut u8,
    pub len: usize,
}

#[repr(C)]
pub struct VeloxStaticResponse {
    pub status: u16,
    pub headers: VeloxBuffer,
    pub body: VeloxBuffer,
    pub error: VeloxBuffer,
}

#[no_mangle]
pub extern "C" fn veloxcore_version() -> VeloxCoreVersion {
    VeloxCoreVersion {
        major: 0,
        minor: 3,
        patch: 0,
    }
}

#[no_mangle]
pub extern "C" fn veloxcore_is_available() -> bool {
    true
}

#[no_mangle]
pub extern "C" fn veloxcore_static_response(
    root: *const c_char,
    target: *const c_char,
    method: *const c_char,
    index: *const c_char,
    keep_alive: bool,
    security_headers: bool,
) -> VeloxStaticResponse {
    match build_static_response(root, target, method, index, keep_alive, security_headers) {
        Ok(response) => response,
        Err(message) => VeloxStaticResponse {
            status: 500,
            headers: empty_buffer(),
            body: empty_buffer(),
            error: into_buffer(message.into_bytes()),
        },
    }
}

#[no_mangle]
pub extern "C" fn veloxcore_parse_request_json(head: *const u8, len: usize) -> VeloxBuffer {
    if head.is_null() {
        return empty_buffer();
    }
    let data = unsafe { std::slice::from_raw_parts(head, len) };
    match parse_request_head(data) {
        Ok(json) => into_buffer(json.into_bytes()),
        Err(message) => into_buffer(format!("{{\"error\":\"{}\"}}", json_escape(&message)).into_bytes()),
    }
}

#[no_mangle]
pub extern "C" fn veloxcore_cache_key(
    template: *const c_char,
    method: *const c_char,
    scheme: *const c_char,
    host: *const c_char,
    uri: *const c_char,
    remote_addr: *const c_char,
) -> VeloxBuffer {
    let template = match c_string(template, "template") {
        Ok(value) => value,
        Err(_) => return empty_buffer(),
    };
    let replacements = [
        ("$protocol", c_string(scheme, "scheme").unwrap_or_default()),
        ("$scheme", c_string(scheme, "scheme").unwrap_or_default()),
        ("$method", c_string(method, "method").unwrap_or_default()),
        ("$host", c_string(host, "host").unwrap_or_default()),
        ("$uri", c_string(uri, "uri").unwrap_or_default()),
        ("$remote_addr", c_string(remote_addr, "remote_addr").unwrap_or_default()),
    ];
    let mut key = template;
    let mut ordered = replacements.to_vec();
    ordered.sort_by(|left, right| right.0.len().cmp(&left.0.len()));
    for (name, value) in ordered {
        key = key.replace(name, &value);
    }
    into_buffer(key.into_bytes())
}

#[no_mangle]
pub extern "C" fn veloxcore_free_buffer(ptr: *mut u8, len: usize) {
    if ptr.is_null() || len == 0 {
        return;
    }
    unsafe {
        drop(Box::from_raw(std::slice::from_raw_parts_mut(ptr, len)));
    }
}

fn parse_request_head(data: &[u8]) -> Result<String, String> {
    let head = std::str::from_utf8(data).map_err(|error| format!("request head is not utf-8: {error}"))?;
    let head = head.strip_suffix("\r\n\r\n").unwrap_or(head);
    let mut lines = head.split("\r\n");
    let request_line = lines.next().ok_or_else(|| "missing request line".to_owned())?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().ok_or_else(|| "missing method".to_owned())?;
    let target = parts.next().ok_or_else(|| "missing target".to_owned())?;
    let version = parts.next().ok_or_else(|| "missing version".to_owned())?;
    if parts.next().is_some() || !version.starts_with("HTTP/") {
        return Err("bad request line".to_owned());
    }

    let mut headers = Vec::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err("bad header line".to_owned());
        };
        headers.push(format!(
            "\"{}\":\"{}\"",
            json_escape(&name.trim().to_ascii_lowercase()),
            json_escape(value.trim())
        ));
    }
    Ok(format!(
        "{{\"method\":\"{}\",\"target\":\"{}\",\"version\":\"{}\",\"headers\":{{{}}}}}",
        json_escape(method),
        json_escape(target),
        json_escape(version),
        headers.join(",")
    ))
}

fn build_static_response(
    root: *const c_char,
    target: *const c_char,
    method: *const c_char,
    index: *const c_char,
    keep_alive: bool,
    security_headers: bool,
) -> Result<VeloxStaticResponse, String> {
    let root = c_string(root, "root")?;
    let target = c_string(target, "target")?;
    let method = c_string(method, "method")?;
    let index = c_string(index, "index")?;
    if method != "GET" && method != "HEAD" {
        return Ok(error_response(405, "Method Not Allowed", keep_alive, security_headers));
    }

    let root = fs::canonicalize(Path::new(&root)).map_err(|error| format!("bad root: {error}"))?;
    let candidate = match resolve_target(&root, &target) {
        Ok(path) => path,
        Err(status) => return Ok(error_response(status, status_text(status), keep_alive, security_headers)),
    };
    let mut path = candidate;
    if path.is_dir() {
        let index_path = path.join(index);
        if index_path.is_file() {
            path = index_path;
        } else {
            return Ok(error_response(403, "Forbidden", keep_alive, security_headers));
        }
    }
    if !path.is_file() {
        return Ok(error_response(404, "Not Found", keep_alive, security_headers));
    }
    let canonical = match fs::canonicalize(&path) {
        Ok(value) => value,
        Err(_) => return Ok(error_response(404, "Not Found", keep_alive, security_headers)),
    };
    if !canonical.starts_with(&root) {
        return Ok(error_response(403, "Forbidden", keep_alive, security_headers));
    }

    let body = match fs::read(&canonical) {
        Ok(value) => value,
        Err(_) => return Ok(error_response(500, "Internal Server Error", keep_alive, security_headers)),
    };
    let content_type = content_type(&canonical);
    let body_len = body.len();
    let mut headers = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {body_len}\r\nContent-Type: {content_type}\r\nConnection: {}\r\n",
        if keep_alive { "keep-alive" } else { "close" }
    );
    append_security_headers(&mut headers, security_headers);
    headers.push_str("\r\n");

    Ok(VeloxStaticResponse {
        status: 200,
        headers: into_buffer(headers.into_bytes()),
        body: if method == "HEAD" {
            empty_buffer()
        } else {
            into_buffer(body)
        },
        error: empty_buffer(),
    })
}

fn c_string(ptr: *const c_char, name: &str) -> Result<String, String> {
    if ptr.is_null() {
        return Err(format!("{name} pointer is null"));
    }
    let text = unsafe { CStr::from_ptr(ptr) };
    text.to_str()
        .map(|value| value.to_owned())
        .map_err(|error| format!("{name} is not utf-8: {error}"))
}

fn resolve_target(root: &Path, target: &str) -> Result<PathBuf, u16> {
    if target.starts_with("http://") || target.starts_with("https://") || target.starts_with("//") {
        return Err(400);
    }
    let path = target.split('?').next().unwrap_or("/");
    if path.contains('\0') || path.to_ascii_lowercase().contains("%00") {
        return Err(400);
    }
    let decoded = percent_decode(path)?;
    let mut clean = PathBuf::from(root);
    for part in decoded.replace('\\', "/").split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            return Err(403);
        }
        let component = Path::new(part);
        if component.components().any(|item| !matches!(item, Component::Normal(_))) {
            return Err(403);
        }
        clean.push(part);
    }
    Ok(clean)
}

fn percent_decode(value: &str) -> Result<String, u16> {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len() {
                return Err(400);
            }
            let high = hex(bytes[index + 1]).ok_or(400u16)?;
            let low = hex(bytes[index + 2]).ok_or(400u16)?;
            output.push(high * 16 + low);
            index += 3;
        } else {
            output.push(bytes[index]);
            index += 1;
        }
    }
    String::from_utf8(output).map_err(|_| 400)
}

fn hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn error_response(status: u16, message: &str, keep_alive: bool, security_headers: bool) -> VeloxStaticResponse {
    let body = format!("{status} {message}\n").into_bytes();
    let body_len = body.len();
    let mut headers = format!(
        "HTTP/1.1 {status} {message}\r\nContent-Length: {body_len}\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: {}\r\n",
        if keep_alive { "keep-alive" } else { "close" }
    );
    append_security_headers(&mut headers, security_headers);
    headers.push_str("\r\n");
    VeloxStaticResponse {
        status,
        headers: into_buffer(headers.into_bytes()),
        body: into_buffer(body),
        error: empty_buffer(),
    }
}

fn status_text(status: u16) -> &'static str {
    match status {
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        500 => "Internal Server Error",
        _ => "Error",
    }
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|value| value.to_str()).unwrap_or_default() {
        "css" => "text/css",
        "gif" => "image/gif",
        "htm" | "html" => "text/html; charset=utf-8",
        "jpeg" | "jpg" => "image/jpeg",
        "js" => "application/javascript",
        "json" => "application/json",
        "png" => "image/png",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        "wasm" => "application/wasm",
        _ => "application/octet-stream",
    }
}

fn append_security_headers(headers: &mut String, enabled: bool) {
    if enabled {
        headers.push_str("X-Content-Type-Options: nosniff\r\n");
        headers.push_str("X-Frame-Options: DENY\r\n");
        headers.push_str("Referrer-Policy: no-referrer\r\n");
    }
}

fn json_escape(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value < ' ' => output.push_str(&format!("\\u{:04x}", value as u32)),
            value => output.push(value),
        }
    }
    output
}

fn into_buffer(data: Vec<u8>) -> VeloxBuffer {
    if data.is_empty() {
        return empty_buffer();
    }
    let len = data.len();
    let mut boxed = data.into_boxed_slice();
    let ptr = boxed.as_mut_ptr();
    std::mem::forget(boxed);
    let buffer = VeloxBuffer {
        ptr,
        len,
    };
    buffer
}

fn empty_buffer() -> VeloxBuffer {
    VeloxBuffer {
        ptr: std::ptr::null_mut(),
        len: 0,
    }
}

#[allow(dead_code)]
fn c_string_lossy(value: &str) -> CString {
    CString::new(value).unwrap_or_else(|_| CString::new("").expect("empty CString"))
}
