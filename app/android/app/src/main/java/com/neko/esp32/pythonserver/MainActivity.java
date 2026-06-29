
package com.neko.esp32.pythonserver;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.format.Formatter;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.webkit.GeolocationPermissions;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.URLEncoder;
import java.util.UUID;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQUEST = 2001;
    private WebView webView;
    private EditText urlInput;
    private SharedPreferences prefs;
    private String phoneId;
    private ValueCallback<Uri[]> filePathCallback;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("settings", MODE_PRIVATE);
        phoneId = buildPhoneId();
        startService(new Intent(this, PythonServerService.class));
        requestLocationPermissionIfNeeded();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(234, 247, 255));
        root.setPadding(18, 18, 18, 18);

        TextView title = new TextView(this);
        title.setText("N.E.K.O_ESP32");
        title.setTextSize(22);
        title.setTextColor(Color.rgb(23, 50, 77));
        title.setGravity(Gravity.CENTER_VERTICAL);
        title.setPadding(0, 0, 0, 6);
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        TextView info = new TextView(this);
        info.setText("ESP32 连接地址请在页面「连接配置」里复制，里面已带配对 Token。\n手机热点或同一 Wi-Fi 下使用: ws://" + localIp() + ":8765/?token=...");
        info.setTextSize(12);
        info.setTextColor(Color.rgb(15, 114, 176));
        info.setPadding(0, 0, 0, 8);
        root.addView(info, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        urlInput = new EditText(this);
        urlInput.setSingleLine(true);
        urlInput.setText("http://localhost:8766");
        urlInput.setHint("http://localhost:8766");
        urlInput.setEnabled(false);
        urlInput.setTextColor(Color.rgb(23, 50, 77));
        urlInput.setHintTextColor(Color.rgb(91, 120, 146));
        row.addView(urlInput, new LinearLayout.LayoutParams(0, -2, 1));

        Button openButton = new Button(this);
        openButton.setText("刷新");
        row.addView(openButton, new LinearLayout.LayoutParams(-2, -2));
        root.addView(row, new LinearLayout.LayoutParams(-1, -2));

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        if (Build.VERSION.SDK_INT >= 21) settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(false);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return request != null && request.getUrl() != null && !isTrustedLocalUrl(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return !isTrustedLocalUrl(url);
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (!isTrustedLocalUrl(view.getUrl())) return false;
                if (filePathCallback != null) filePathCallback.onReceiveValue(null);
                filePathCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER_REQUEST);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                callback.invoke(origin, isTrustedLocalUrl(origin), false);
            }
        });
        root.addView(webView, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(root);

        openButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                loadServer();
            }
        });

        handler.postDelayed(new Runnable() {
            @Override public void run() {
                loadServer();
            }
        }, 1200);
    }

    private void loadServer() {
        String url = "http://localhost:8766";
        urlInput.setText(url);
        prefs.edit().putString("server_url", url).apply();
        try {
            InputMethodManager imm = (InputMethodManager)getSystemService(Context.INPUT_METHOD_SERVICE);
            imm.hideSoftInputFromWindow(urlInput.getWindowToken(), 0);
        } catch (Exception ignored) {}
        webView.loadUrl(withPhoneId(url));
    }

    private boolean isTrustedLocalUrl(String url) {
        if (url == null) return false;
        try {
            Uri uri = Uri.parse(url);
            String scheme = uri.getScheme();
            String host = uri.getHost();
            int port = uri.getPort();
            boolean localHost = "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "::1".equals(host);
            boolean localPort = port == -1 || port == 8766;
            return "http".equalsIgnoreCase(scheme) && localHost && localPort;
        } catch (Exception ignored) {
            return false;
        }
    }

    private String buildPhoneId() {
        String id = Settings.Secure.getString(getContentResolver(), Settings.Secure.ANDROID_ID);
        if (id == null || id.length() == 0) {
            id = prefs.getString("phone_id_fallback", "");
            if (id.length() == 0) {
                id = UUID.randomUUID().toString().replace("-", "");
                prefs.edit().putString("phone_id_fallback", id).apply();
            }
        }
        return "PHONE-ANDROID-" + id.toUpperCase();
    }

    private String withPhoneId(String url) {
        try {
            String base = url;
            String hash = "";
            int hashPos = url.indexOf('#');
            if (hashPos >= 0) {
                base = url.substring(0, hashPos);
                hash = url.substring(hashPos);
            }
            String sep = base.contains("?") ? "&" : "?";
            return base + sep + "phone_id=" + URLEncoder.encode(phoneId, "UTF-8") + hash;
        } catch (Exception ignored) {
            return url;
        }
    }

    private String localIp() {
        try {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            int ip = wm.getConnectionInfo().getIpAddress();
            String formatted = Formatter.formatIpAddress(ip);
            if (formatted != null && formatted.length() > 0 && !formatted.equals("0.0.0.0")) return formatted;
        } catch (Exception ignored) {}
        return "手机IP";
    }

    private void requestLocationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, 3001);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && filePathCallback != null) {
            Uri[] result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            filePathCallback.onReceiveValue(result);
            filePathCallback = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
