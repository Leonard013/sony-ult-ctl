APP = SonyULTMenu
SRC = SonyULTMenu.swift

$(APP).app: $(SRC) Info.plist
	@mkdir -p $(APP).app/Contents/MacOS
	@cp Info.plist $(APP).app/Contents/
	swiftc -O -o $(APP).app/Contents/MacOS/$(APP) $(SRC) \
		-framework IOBluetooth -framework Cocoa
	@echo "Built $(APP).app"

run: $(APP).app
	@open $(APP).app

clean:
	@rm -rf $(APP).app

.PHONY: run clean
