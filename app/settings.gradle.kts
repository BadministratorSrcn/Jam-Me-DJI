pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
        // MSDK V5 AARs are placed app/libs (see docs/ARCHITECTURE.md §4)
        // or fetched via the official sample's repository setup.
        flatDir { dirs("app/libs") }
    }
}
rootProject.name = "JamMeDJI"
include(":app")
