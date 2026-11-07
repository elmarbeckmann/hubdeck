import UIKit

// No window and no scene manifest: the app only runs the HomeKit bridge.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    private var server: Server?
    private var home: Home?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let server = Server()
        home = Home(server: server)
        server.start()
        self.server = server
        return true
    }
}
