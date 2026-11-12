import CarPlay
import UIKit

/// CarPlay: the garage doors, the front door and house modes as one list.
/// Only used once Apple grants the CarPlay Driving Task entitlement (project-carplay.yml adds it and the scene);
/// until then iOS 26 shows the "Garage and door" widget on CarPlay's widget screen instead.
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var interfaceController: CPInterfaceController?

    func templateApplicationScene(_ scene: CPTemplateApplicationScene, didConnect controller: CPInterfaceController) {
        interfaceController = controller
        Task { @MainActor in
            await HomeStore.shared.loadHome()
            controller.setRootTemplate(self.makeList(), animated: false, completion: nil)
        }
    }

    func templateApplicationScene(_ scene: CPTemplateApplicationScene, didDisconnectInterfaceController controller: CPInterfaceController) {
        interfaceController = nil
    }

    @MainActor private func makeList() -> CPListTemplate {
        let home = HomeStore.shared.home
        var doorItems: [CPListItem] = []
        for g in home?.devices(kind: .garage) ?? [] {
            let open = g["open"]?.bool == true
            let item = CPListItem(text: g.name, detailText: open ? "Open · tap to close" : "Closed · tap to open",
                                  image: UIImage(systemName: open ? "door.garage.open" : "door.garage.closed"))
            item.handler = { [weak self] _, done in
                self?.run("garage.set", target: g.id, params: ["open": !open]) { done() }
            }
            doorItems.append(item)
        }
        for l in home?.devices(kind: .lock) ?? [] {
            let locked = l["locked"]?.bool ?? true
            let item = CPListItem(text: l.name, detailText: locked ? "Locked" : "Unlocked · tap to lock",
                                  image: UIImage(systemName: locked ? "lock.fill" : "lock.open.fill"))
            item.handler = { [weak self] _, done in
                if locked { done(); return }
                self?.run("lock.set", target: l.id, params: ["locked": true]) { done() }
            }
            doorItems.append(item)
        }
        let modeItems: [CPListItem] = (home?.modes ?? []).map { m in
            let item = CPListItem(text: m.name, detailText: nil, image: UIImage(systemName: m.symbol ?? "sparkles"))
            item.handler = { [weak self] _, done in self?.run("mode.run", target: m.id, params: [:]) { done() } }
            return item
        }
        var sections = [CPListSection(items: doorItems, header: "Doors", sectionIndexTitle: nil)]
        if !modeItems.isEmpty { sections.append(CPListSection(items: modeItems, header: "Modes", sectionIndexTitle: nil)) }
        return CPListTemplate(title: "Thomas Creek", sections: sections)
    }

    private func run(_ action: String, target: String, params: [String: Any], done: @escaping () -> Void) {
        Task { @MainActor in
            do {
                _ = try await HubClient.shared.action(action, target: target, params: params, quick: true)
                try? await Task.sleep(for: .seconds(1))
                await HomeStore.shared.loadHome()
            } catch {}
            if let c = interfaceController { c.setRootTemplate(makeList(), animated: false, completion: nil) }
            done()
        }
    }
}
