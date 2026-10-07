# NanoHTTPD
-keep class fi.iki.elonen.** { *; }

# Room
-keep class * extends androidx.room.RoomDatabase
-keep @androidx.room.Entity class *
