import sys, gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk, GLib
n=int(sys.argv[1]); GLib.set_prgname("bigtree")
s=Gtk.ListStore(str,str)
for i in range(1,n+1): s.append([f"Row {i:05d}", f"value {i*7%1000}"])
v=Gtk.TreeView(model=s)
for k,t in enumerate(("Name","Value")): v.append_column(Gtk.TreeViewColumn(t,Gtk.CellRendererText(),text=k))
w=Gtk.Window(title="Big Tree"); b=Gtk.Box(orientation=1); sw=Gtk.ScrolledWindow(); sw.add(v)
b.pack_start(sw,True,True,0); b.pack_start(Gtk.Button(label="Apply selection"),False,False,0)
w.add(b); w.set_default_size(900,700); w.show_all(); w.connect("destroy",Gtk.main_quit); Gtk.main()
