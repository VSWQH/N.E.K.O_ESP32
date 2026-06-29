
package com.neko.esp32.pythonserver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class PythonServerService extends Service {
    public static final String CHANNEL_ID = "neko_esp32_server";
    private static boolean started = false;

    @Override
    public void onCreate() {
        super.onCreate();
        startForeground(1001, buildNotification("Python 服务端运行中"));
        startPythonServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startPythonServer();
        return START_STICKY;
    }

    private void startPythonServer() {
        if (started) return;
        started = true;
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    if (!Python.isStarted()) {
                        Python.start(new AndroidPlatform(PythonServerService.this));
                    }
                    PyObject runner = Python.getInstance().getModule("android_runner");
                    runner.callAttr("start", "0.0.0.0", 8766, 8765);
                } catch (Throwable t) {
                    started = false;
                    Log.e("NEKO_ESP32", "Python server failed", t);
                }
            }
        }, "neko-python-server").start();
    }

    private Notification buildNotification(String text) {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL_ID, getString(R.string.server_channel), NotificationManager.IMPORTANCE_LOW);
            manager.createNotificationChannel(channel);
            return new Notification.Builder(this, CHANNEL_ID)
                    .setSmallIcon(R.mipmap.ic_launcher)
                    .setContentTitle("N.E.K.O_ESP32")
                    .setContentText(text)
                    .setOngoing(true)
                    .build();
        }
        return new Notification.Builder(this)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("N.E.K.O_ESP32")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
