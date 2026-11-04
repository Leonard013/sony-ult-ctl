APP = SonyULTMenu
SRC = SonyULTMenu.swift

$(APP).app: $(SRC) Info.plist AppIcon.icns
	@mkdir -p $(APP).app/Contents/MacOS $(APP).app/Contents/Resources
	@cp Info.plist $(APP).app/Contents/
	@cp AppIcon.icns $(APP).app/Contents/Resources/
	swiftc -O -o $(APP).app/Contents/MacOS/$(APP) $(SRC) \
		-framework IOBluetooth -framework Cocoa -framework SwiftUI
	@# Sign the whole bundle ad-hoc so Info.plist binds and TCC can read
	@# NSBluetoothAlwaysUsageDescription + anchor the permission grant.
	codesign --force --sign - --identifier com.leonard013.sonyultmenu $(APP).app
	@echo "Built $(APP).app"

run: $(APP).app
	@open $(APP).app

clean:
	@rm -rf $(APP).app

.PHONY: run clean
