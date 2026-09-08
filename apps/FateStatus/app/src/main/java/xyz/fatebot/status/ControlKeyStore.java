package xyz.fatebot.status;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class ControlKeyStore {
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final String KEY_ALIAS = "fate_control_key_v1";
    private static final String PREF_ENCRYPTED_KEY = "control_key_encrypted";
    private static final String PREF_ENCRYPTED_IV = "control_key_iv";
    private static final String PREF_ACTIVE_PROFILE = "bot_profile_active";

    private ControlKeyStore() {}

    static synchronized void save(Context context, String value) throws Exception {
        String profileId = preferences(context).getString(PREF_ACTIVE_PROFILE, "");
        save(context, profileId, value);
    }

    static synchronized void save(Context context, String profileId, String value) throws Exception {
        String keyValue = value == null ? "" : value.trim();
        SharedPreferences prefs = preferences(context);
        String encryptedKey = scoped(PREF_ENCRYPTED_KEY, profileId);
        String encryptedIv = scoped(PREF_ENCRYPTED_IV, profileId);
        if (keyValue.isEmpty()) {
            prefs.edit()
                .remove(encryptedKey)
                .remove(encryptedIv)
                .remove(FateWidgetProvider.PREF_CONTROL_KEY)
                .apply();
            return;
        }

        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, encryptionKey());
        byte[] ciphertext = cipher.doFinal(keyValue.getBytes(StandardCharsets.UTF_8));
        prefs.edit()
            .putString(encryptedKey, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
            .putString(encryptedIv, Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
            .remove(FateWidgetProvider.PREF_CONTROL_KEY)
            .apply();
    }

    static synchronized String read(Context context) {
        SharedPreferences prefs = preferences(context);
        return read(context, prefs.getString(PREF_ACTIVE_PROFILE, ""));
    }

    static synchronized String read(Context context, String profileId) {
        SharedPreferences prefs = preferences(context);
        String ciphertext = prefs.getString(scoped(PREF_ENCRYPTED_KEY, profileId), "");
        String iv = prefs.getString(scoped(PREF_ENCRYPTED_IV, profileId), "");
        if (!ciphertext.isEmpty() && !iv.isEmpty()) {
            try {
                Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
                cipher.init(
                    Cipher.DECRYPT_MODE,
                    encryptionKey(),
                    new GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP))
                );
                return new String(
                    cipher.doFinal(Base64.decode(ciphertext, Base64.NO_WRAP)),
                    StandardCharsets.UTF_8
                );
            } catch (Exception ignored) {
                return "";
            }
        }

        if (profileId != null && !profileId.trim().isEmpty()) return "";

        String legacy = prefs.getString(FateWidgetProvider.PREF_CONTROL_KEY, "").trim();
        if (!legacy.isEmpty()) {
            try {
                save(context, legacy);
            } catch (Exception ignored) {
                // Keep the existing private preference readable until migration succeeds.
            }
        }
        return legacy;
    }

    static synchronized void delete(Context context, String profileId) {
        preferences(context).edit()
            .remove(scoped(PREF_ENCRYPTED_KEY, profileId))
            .remove(scoped(PREF_ENCRYPTED_IV, profileId))
            .apply();
    }

    private static String scoped(String key, String profileId) {
        String id = profileId == null ? "" : profileId.trim();
        return id.isEmpty() ? key : key + "_profile_" + id;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE);
    }

    private static SecretKey encryptionKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(KEYSTORE);
        keyStore.load(null);
        SecretKey existing = (SecretKey) keyStore.getKey(KEY_ALIAS, null);
        if (existing != null) return existing;

        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setRandomizedEncryptionRequired(true)
            .build());
        return generator.generateKey();
    }
}
